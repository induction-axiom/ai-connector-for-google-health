"""The MCP tools: a read-only pass-through to the Google Health API, one tool per API method.

Answers are Google's JSON, unchanged and compact. The connector adds only what keeps a call
usable: an answer too large for the AI's context comes back as result_too_large with how to
ask for less, and describe_data_type reads each type's schema from Google's discovery
document. Expected failures come back as results with error_code and next_action, not tool
errors, so the AI learns what to do next.
"""
import json
import re
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from google_health import GoogleHealthError

# About 15,000 tokens of JSON: room for the AI to read it and still think.
MAX_RESULT_CHARS = 60_000

DataType = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$", description=(
    "A kebab-case data type, such as sleep, heart-rate or daily-resting-heart-rate. "
    "describe_data_type with no data_type lists them all."))]
Filter = Annotated[str, Field(max_length=2000, description=(
    'An AIP-160 time filter, such as sleep.interval.civil_end_time >= "2026-10-01". Field '
    "names are snake_case (heart_rate), unlike the data type. describe_data_type gives the "
    "fields each type supports."))]
PageSize = Annotated[int, Field(ge=1, le=10000, description=(
    "At most this many points. Google's default is 1440, and 25 for sleep and exercise, "
    "which allow no more."))]
PageToken = Annotated[str, Field(max_length=4096, description=(
    "nextPageToken from the previous answer; keep every other argument the same"))]
SourceFamily = Annotated[Literal["all-sources", "google-wearables", "google-sources", "self-sources"],
    Field(description=("all-sources is the default. google-wearables: Fitbit and Pixel Watch "
                       "trackers only, no manual logs. google-sources: Google's own, including "
                       "manual logs and Health Connect."))]
Fields = Annotated[str, Field(max_length=1000, pattern=r"^[A-Za-z0-9_,/()*]+$", description=(
    "Google's partial-response mask, to get only what you need, such as "
    "dataPoints(name,sleep(interval,summary)),nextPageToken"))]
RESOURCES = ("users/me/identity, users/me/profile, users/me/settings, users/me/pairedDevices/{id}, "
             "or a data point's name, users/me/dataTypes/{data_type}/dataPoints/{id}")
ResourceName = Annotated[str, Field(max_length=300, description=RESOURCES)]
# What get_resource may read: the user resources the granted scopes cover, and single items.
READABLE = re.compile(r"users/me/(identity|profile|settings|pairedDevices/[A-Za-z0-9_.~-]{1,200}"
                      r"|dataTypes/[a-z][a-z0-9-]{0,63}/dataPoints/[A-Za-z0-9_.~-]{1,200})")

NEXT_ACTIONS = {
    "not_connected": "connect_google_health", "google_health_disconnected": "connect_google_health",
    "client_invalid": "connect_google_health", "google_unavailable": "retry_later",
    "google_rejected": "tell_owner", "rate_limited": "retry_later",
    "request_rejected": "fix_arguments", "not_found": "check_ids", "unknown_data_type": "fix_arguments",
    "permission_denied": "tell_owner", "invalid_arguments": "fix_arguments",
    "result_too_large": "ask_for_less",
}
# The codes that need the owner to act in the dashboard.
OWNER_ACTIONS = {"not_connected", "google_health_disconnected", "client_invalid", "permission_denied"}

# What the schemas don't say, learned from Google's guides. The only hand-kept part: when the
# API changes, a note may go stale, but no tool breaks.
NOTES = {
    "sleep": [
        "Use reconcile_data_points: list_data_points returns each source's copy, so one night can appear twice.",
        "Filter on sleep.interval.civil_end_time (the local date the sleep ends) or "
        "sleep.interval.end_time; sleep has no start-time filter.",
        "At most 25 sleeps per page. For many nights, leave out the stages with fields such as "
        "dataPoints(name,sleep(interval,type,summary,metadata)),nextPageToken, then read single nights in full.",
        "type CLASSIC has AWAKE, RESTLESS and ASLEEP stages; STAGES has AWAKE, LIGHT, DEEP and REM. "
        "Don't compare stage minutes across the two.",
        "shortAwakenings and outOfBedSegments overlap the stages: adding them to stage minutes counts that time twice.",
        "A day can have several sleeps: metadata.mainSleep marks the main one, metadata.nap the naps.",
        "minutesToFallAsleep is often 0 for automatically detected sleep; it isn't a measured sleep latency.",
    ],
    "heart-rate": [
        "Kept per second: one night is tens of thousands of points. Use roll_up_data_points with a "
        "window_size such as 300s (at most 14 days per request) rather than listing points.",
    ],
    "heart-rate-variability": ["Single readings, mostly during sleep. daily-heart-rate-variability is the nightly summary."],
    "oxygen-saturation": ["Kept per minute, mostly during sleep. Invalid readings are dropped, so gaps are normal."],
    "respiratory-rate-sleep-summary": ["One summary per sleep. daily-respiratory-rate is the daily value."],
    "daily-sleep-temperature-derivations": ["Nightly skin temperature relative to the owner's own baseline."],
    "steps": ["Kept per minute. Use roll_up_data_points or daily_roll_up_data_points for totals."],
}
GENERAL_NOTES = [
    "Every time has a physical form (UTC, with its utcOffset) and a civil form (the owner's local "
    "clock). Daily types and daily_roll_up_data_points use civil dates.",
    "int64 counts such as minutesAsleep are strings in JSON. Durations are strings such as \"300s\".",
    "Answers over the size limit come back as result_too_large: ask with a smaller page_size, a "
    "shorter range, fields, or a roll-up.",
    "roll_up_data_points covers at most 14 days for heart-rate, active-minutes, total-calories and "
    "calories-in-heart-rate-zone, and 90 days for other types.",
]


class Failure(Exception):
    """A tool's expected failure; run() turns it into a result for the AI."""

    def __init__(self, code, detail=None, **extra):
        super().__init__(code)
        self.code, self.detail, self.extra = code, detail, extra


# ---------- The discovery document ----------

def kebab(field):
    """dailyVo2Max → daily-vo2-max: the API's own spelling of a data type in paths."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", field).lower()


def catalog(document):
    """Every data type the discovery document names, from the fields of DataPoint and of
    the two roll-up answers, which say which collection each field is for."""
    schemas = document["schemas"]
    types = {}
    for field, prop in schemas["DataPoint"]["properties"].items():
        if "$ref" not in prop or field == "dataSource":
            continue
        found = re.search(r"`([a-z0-9-]+)` (\w+) data type", prop.get("description", ""))
        types[found[1] if found else kebab(field)] = {
            "field": field, "kind": found[2] if found else None, "schema": prop["$ref"],
            "description": prop.get("description")}
    for answer, schema in (("roll_up", "RollupDataPoint"), ("daily_roll_up", "DailyRollupDataPoint")):
        for field, prop in schemas[schema]["properties"].items():
            if prop.get("$ref", "CivilDateTime") == "CivilDateTime":
                continue  # the window's own start and end
            found = re.search(r"from the `([a-z0-9-]+)` data type", prop.get("description", ""))
            entry = types.setdefault(found[1] if found else kebab(field), {"kind": "roll-up only"})
            entry[answer] = {"field": field, "schema": prop["$ref"]}
    return types


def referenced(schemas, names):
    """The named schemas and every schema they refer to, each once, in Google's own words."""
    found, pending = {}, list(names)
    while pending:
        name = pending.pop()
        if name in found or name not in schemas:
            continue
        found[name] = shape(schemas[name])
        pending += re.findall(r'"\$ref": "([A-Za-z0-9]+)"', json.dumps(schemas[name]))
    return found


def shape(node):
    """A schema node without the discovery document's bookkeeping, enums with their meanings."""
    if "$ref" in node:
        return {"$ref": node["$ref"], **({"description": node["description"]} if "description" in node else {})}
    out = {k: node[k] for k in ("description", "type", "format") if k in node}
    if "enum" in node:
        out["enum"] = dict(zip(node["enum"], node.get("enumDescriptions", [""] * len(node["enum"]))))
    if "items" in node:
        out["items"] = shape(node["items"])
    if "properties" in node:
        out["properties"] = {k: shape(v) for k, v in node["properties"].items()}
    return out


def method(document, *path):
    resource = document
    for name in path[:-1]:
        resource = resource["resources"][name]
    return resource["methods"][path[-1]]


def register(server, health, owner_url, connector, security):
    """Add the tools to server. Returns them by name, for the dashboard's preview."""
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=True)

    def run(work):
        try:
            result = {"ok": True, **work()}
        except (GoogleHealthError, Failure) as error:
            result = {"ok": False, "error_code": error.code,
                      "next_action": NEXT_ACTIONS.get(error.code, "tell_owner")}
            if isinstance(error, Failure):
                result.update(error.extra)
            if error.detail:
                result["detail"] = error.detail
            if error.code in OWNER_ACTIONS:
                result["owner_url"] = owner_url
        text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = json.dumps(too_large(result, len(text)), separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)])

    def too_large(result, size):
        items = next((v for v in result.values() if isinstance(v, list)), [])
        answer = {"ok": False, "error_code": "result_too_large", "next_action": "ask_for_less",
                  "result_chars": size, "limit_chars": MAX_RESULT_CHARS, "items_in_answer": len(items),
                  "detail": "Ask for less: a smaller page_size, then page_token for more; a shorter "
                            "time range; fields to keep only what you need; or a roll-up."}
        if len(items) > 1:
            answer["page_size_that_fits"] = max(1, len(items) * MAX_RESULT_CHARS * 9 // (size * 10))
        return answer

    def data_type_path(data_type):
        return f"users/me/dataTypes/{data_type}/dataPoints"

    def family(name):
        return name and "users/me/dataSourceFamilies/" + name

    def query(data_type, filter, page_size, page_token, data_source_family, fields):
        return {"filter": filter, "pageSize": page_size, "pageToken": page_token,
                "dataSourceFamily": family(data_source_family), "fields": fields}

    def roll_up(path, data_type, body, fields):
        body = {k: v for k, v in body.items() if v is not None}
        return health.call("POST", data_type_path(data_type) + path, {"fields": fields}, body)

    @server.tool(annotations=read, meta=security)
    def describe_data_type(data_type: DataType | None = None) -> CallToolResult:
        """What the Google Health API holds and how to ask for it, from Google's own
        discovery document. Call it before reading a data type for the first time.

        Without data_type: every data type, with its kind (interval, sample, session, daily,
        or roll-up only), plus general notes. With data_type: its schema and every schema it
        refers to, the roll-ups it supports, the filter syntax, and notes on reading it right.

        owner_url is this deployment's own dashboard, where Google Health is connected. Give
        the owner that exact clickable link when they ask where to manage the connector.
        """
        def work():
            document = health.discovery()
            types = catalog(document)
            if data_type is None:
                return {"data_types": {name: entry.get("kind") for name, entry in sorted(types.items())},
                        "notes": GENERAL_NOTES, "api_revision": document.get("revision"),
                        "owner_url": owner_url, "connector": connector}
            if data_type not in types:
                raise Failure("unknown_data_type", "Data types: " + ", ".join(sorted(types)))
            entry = types[data_type]
            names = [entry["schema"]] if "schema" in entry else []
            names += [entry[a]["schema"] for a in ("roll_up", "daily_roll_up") if a in entry]
            listing = method(document, "users", "dataTypes", "dataPoints", "list")["parameters"]
            return {"data_type": data_type, "kind": entry.get("kind"),
                    "data_point_field": entry.get("field"), "data_point_schema": entry.get("schema"),
                    "filter_name": data_type.replace("-", "_"),
                    **{a: entry[a] for a in ("roll_up", "daily_roll_up") if a in entry},
                    "notes": NOTES.get(data_type, []) + GENERAL_NOTES,
                    "filter_syntax": listing["filter"]["description"],
                    "schemas": referenced(document["schemas"], names + ["DataSource"])}
        return run(work)

    @server.tool(annotations=read, meta=security)
    def list_data_points(data_type: DataType, filter: Filter | None = None, page_size: PageSize | None = None,
                         page_token: PageToken | None = None,
                         data_source_family: SourceFamily | None = None,
                         fields: Fields | None = None) -> CallToolResult:
        """List a data type's points as stored, from every source, newest first.

        Google's ListDataPoints, unchanged. Two devices that record the same thing give two
        points: for sleep, and anything else more than one device records, prefer
        reconcile_data_points. Continue with nextPageToken as page_token.
        """
        return run(lambda: health.call("GET", data_type_path(data_type),
                                       query(data_type, filter, page_size, page_token, data_source_family, fields)))

    @server.tool(annotations=read, meta=security)
    def reconcile_data_points(data_type: DataType, filter: Filter | None = None, page_size: PageSize | None = None,
                              page_token: PageToken | None = None,
                              data_source_family: SourceFamily | None = None,
                              fields: Fields | None = None) -> CallToolResult:
        """List a data type's points with overlapping ones from different sources merged
        into one, as the Google Health app shows them.

        Google's ReconcileDataPoints, unchanged. Continue with nextPageToken as page_token.
        """
        return run(lambda: health.call("GET", data_type_path(data_type) + ":reconcile",
                                       query(data_type, filter, page_size, page_token, data_source_family, fields)))

    @server.tool(annotations=read, meta=security)
    def roll_up_data_points(
        data_type: DataType,
        range: Annotated[dict[str, Any], Field(description=(
            'UTC timestamps, end exclusive: {"startTime": "2026-10-01T00:00:00Z", "endTime": "2026-10-02T00:00:00Z"}'))],
        window_size: Annotated[str, Field(pattern=r"^\d{1,9}(\.\d{1,9})?s$", description=(
            'Each window\'s length in seconds, such as "300s"'))],
        data_source_family: SourceFamily | None = None, page_size: PageSize | None = None,
        page_token: PageToken | None = None, fields: Fields | None = None,
    ) -> CallToolResult:
        """Aggregate a data type into fixed windows of physical time, computed by Google:
        for example average, minimum and maximum heart rate per 5 minutes.

        Google's RollUpDataPoints, unchanged. describe_data_type says which types roll up
        and what each window holds. Continue with nextPageToken as page_token.
        """
        return run(lambda: roll_up(":rollUp", data_type, {
            "range": range, "windowSize": window_size, "dataSourceFamily": family(data_source_family),
            "pageSize": page_size, "pageToken": page_token}, fields))

    @server.tool(annotations=read, meta=security)
    def daily_roll_up_data_points(
        data_type: DataType,
        range: Annotated[dict[str, Any], Field(description=(
            'Local dates, end exclusive: {"start": {"date": {"year": 2026, "month": 10, "day": 1}}, '
            '"end": {"date": {"year": 2026, "month": 11, "day": 1}}}'))],
        window_size_days: Annotated[int, Field(ge=1, le=90)] = 1,
        data_source_family: SourceFamily | None = None, page_size: PageSize | None = None,
        page_token: PageToken | None = None, fields: Fields | None = None,
    ) -> CallToolResult:
        """Aggregate a data type by the owner's calendar days, computed by Google: for
        example steps per day, or weekly with window_size_days 7.

        Google's DailyRollUpDataPoints, unchanged. The range must start at the start of a
        window. Continue with nextPageToken as page_token.
        """
        return run(lambda: roll_up(":dailyRollUp", data_type, {
            "range": range, "windowSizeDays": window_size_days,
            "dataSourceFamily": family(data_source_family), "pageSize": page_size,
            "pageToken": page_token}, fields))

    @server.tool(annotations=read, meta=security)
    def get_resource(name: ResourceName, fields: Fields | None = None) -> CallToolResult:
        """Read one resource by name: the owner's identity, profile (age, height, stride)
        or settings (units, time zone), a paired device, or a single data point.

        Google's Get methods, unchanged.
        """
        def work():
            if not READABLE.fullmatch(name):
                raise Failure("invalid_arguments", "Readable: " + RESOURCES)
            return health.call("GET", name, {"fields": fields})
        return run(work)

    @server.tool(annotations=read, meta=security)
    def list_paired_devices(page_size: Annotated[int, Field(ge=1, le=100)] = 50,
                            page_token: PageToken | None = None) -> CallToolResult:
        """The trackers and watches paired with the owner's account, with their battery and
        last sync: where the data comes from, and whether it is recent.

        Google's ListPairedDevices, unchanged.
        """
        return run(lambda: health.call("GET", "users/me/pairedDevices",
                                       {"pageSize": page_size, "pageToken": page_token}))

    return {"describe_data_type": describe_data_type, "reconcile_data_points": reconcile_data_points,
            "get_resource": get_resource, "list_paired_devices": list_paired_devices}
