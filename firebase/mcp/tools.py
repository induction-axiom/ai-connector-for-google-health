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

from google_health import SCOPES, GoogleHealthError

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
    "At most this many points, for most types. Google's default varies (50 for heart-rate, 25 "
    "for sleep); sleep and exercise allow no more than 25."))]
PageToken = Annotated[str, Field(max_length=4096, description=(
    "nextPageToken from the previous answer; keep every other argument the same"))]
SourceFamily = Annotated[Literal["all-sources", "google-wearables", "google-sources", "self-sources"],
    Field(description=("all-sources is the default. google-wearables: Fitbit and Pixel Watch "
                       "trackers only, no manual logs. google-sources: Google's own, including "
                       "manual logs and Health Connect."))]
Fields = Annotated[str, Field(max_length=1000, pattern=r"^[A-Za-z0-9_,/()*]+$", description=(
    "Google's partial-response mask, to get only what you need. Each tool's answer has its own "
    "field names: describe_data_type gives a working mask for each."))]
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
    "result_too_large": "ask_for_less", "not_readable": "use_another_data_type",
}
# The codes that need the owner to act in the dashboard.
OWNER_ACTIONS = {"not_connected", "google_health_disconnected", "client_invalid", "permission_denied"}

# The data types that need a scope area of their own, from Google's table of scopes. Without
# it, Google refuses them in several ways (403, 503, or "List is not supported"), so the tools
# say so without asking for any left out of google_health.SCOPES.
SCOPE_AREAS = {
    "electrocardiogram": "ecg", "irregular-rhythm-notification": "irn",
    "food": "nutrition", "food-measurement-unit": "nutrition", "hydration-log": "nutrition",
    "nutrition-log": "nutrition", "moods": "mindfulness", "menstrual-period": "reproductive_health",
    "ovulation-test": "reproductive_health", "symptoms": "logged_symptoms",
}
OUTSIDE_SCOPES = {data_type: area for data_type, area in SCOPE_AREAS.items()
                  if not any(f".{area}." in scope for scope in SCOPES)}

# Everything below is what the schemas don't say, measured against the live API on 2026-10-24
# or taken from Google's guides. It is the only hand-kept part: when the API changes, an entry
# may go stale, but no tool breaks, and Google's own errors still say what went wrong.

# Answers the discovery document describes but Google refuses; its error names what the type
# does support.
REFUSED = {
    "floors": {"list_data_points"},
    "activity-level": {"roll_up_data_points", "daily_roll_up_data_points"},
    "daily-heart-rate-variability": {"daily_roll_up_data_points"},
    "daily-resting-heart-rate": {"daily_roll_up_data_points"},
}
NOTES = {
    "sleep": [
        "Use reconcile_data_points: list_data_points returns each source's copy, so one night can appear twice.",
        "Filter on sleep.interval.civil_end_time (the local date the sleep ends) or "
        "sleep.interval.end_time; sleep has no start-time filter.",
        "A night with its stages is about 6 to 7 KB, so a full page of 25 is over the size limit. For "
        "many nights, leave out the stages with fields such as "
        "dataPoints(dataPointName,sleep(interval,type,summary,metadata)),nextPageToken, then read single nights in full.",
        "type CLASSIC has AWAKE, RESTLESS and ASLEEP stages; STAGES has AWAKE, LIGHT, DEEP and REM. "
        "Don't compare stage minutes across the two.",
        "shortAwakenings and outOfBedSegments overlap the stages: adding them to stage minutes counts that time twice.",
        "A day can have several sleeps: metadata.mainSleep marks the main one, metadata.nap the naps.",
        "minutesToFallAsleep is often 0 for automatically detected sleep; it isn't a measured sleep latency.",
    ],
    "heart-rate": [
        "Dense: about 325 bytes a point, so a page of 1440 is about 470 KB. Use roll_up_data_points "
        "with a window_size such as 300s (at most 14 days per request) rather than listing points.",
        "To list points anyway, drop the civil times with fields such as "
        "dataPoints(heartRate(beatsPerMinute,sampleTime(physicalTime,utcOffset))),nextPageToken.",
    ],
    "heart-rate-variability": ["Single readings, mostly during sleep. daily-heart-rate-variability is the nightly summary."],
    "oxygen-saturation": [
        "Kept per minute, mostly during sleep. Invalid readings are dropped, so gaps are normal.",
        "A page is about a night of readings whatever page_size asks; narrow by time range instead.",
        "reconcile_data_points can answer with empty pages here; list_data_points has the readings.",
    ],
    "respiratory-rate-sleep-summary": ["One summary per sleep. daily-respiratory-rate is the daily value."],
    "daily-sleep-temperature-derivations": ["Nightly skin temperature relative to the owner's own baseline."],
    "steps": ["Kept per minute. Use roll_up_data_points or daily_roll_up_data_points for totals."],
}
# Google ignores page_size for these: oxygen-saturation answers about a night of readings
# (some 470) whatever page_size asks.
UNPAGED = {"oxygen-saturation"}
GENERAL_NOTES = [
    "Sessions (sleep, exercise) give times only in UTC, with their utcOffset. Samples, intervals and "
    "daily types also give civil times (the owner's local clock) as {date: {year, month, day}, time: "
    "{hours, minutes, seconds}}, about a third of each point: leave them out with fields when UTC is enough.",
    "Only some types give a point an ID (sleep, exercise, weight, height, body-fat, "
    "respiratory-rate-sleep-summary): name in list_data_points, dataPointName in reconcile_data_points.",
    "Keep paging while nextPageToken is present: pages are often short, and can be empty. page_size "
    "is a maximum for most types, but oxygen-saturation always answers about a night of readings, "
    "and steps, distance and active-minutes have at times answered hundreds of points for a page_size "
    "of 5: narrow those by time range.",
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
    types, by_field = {}, {}
    for field, prop in schemas["DataPoint"]["properties"].items():
        if "$ref" not in prop or field == "dataSource":
            continue
        found = re.search(r"`([a-z0-9-]+)` (\w+) data type", prop.get("description", ""))
        by_field[field] = found[1] if found else kebab(field)
        types[by_field[field]] = {
            "field": field, "kind": found[2] if found else None, "schema": prop["$ref"],
            "description": prop.get("description")}
    # ReconcileDataPoints answers in its own point, which holds only the types it merges.
    for field, prop in schemas["ReconciledDataPoint"]["properties"].items():
        if "$ref" in prop and field in by_field:
            types[by_field[field]]["reconcile"] = True
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


def envelope(schema, field):
    """A point's own fields, and only this data type's field of the many it may hold."""
    return {**shape({k: v for k, v in schema.items() if k != "properties"}), "properties": {
        k: shape(v) for k, v in schema["properties"].items() if "$ref" not in v or k in {field, "dataSource"}}}


def method(document, *path):
    resource = document
    for name in path[:-1]:
        resource = resource["resources"][name]
    return resource["methods"][path[-1]]


def register(server, health, owner_url, connector, security):
    """Add the tools to server. Returns them by name, for the dashboard's preview."""
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                           openWorldHint=True)

    def run(work, fields=None, page_size=None):
        try:
            result = {"ok": True, **work()}
        except (GoogleHealthError, Failure) as error:
            result = {"ok": False, "error_code": error.code,
                      "next_action": NEXT_ACTIONS.get(error.code, "tell_owner")}
            if isinstance(error, Failure):
                result.update(error.extra)
            if error.detail:
                result["detail"] = error.detail
            if error.code == "request_rejected" and fields:
                # Google names no argument; a mask with another tool's field names is the usual cause.
                result["hint"] = "Check fields against the fields_example describe_data_type gives for this tool."
            if error.code in OWNER_ACTIONS:
                result["owner_url"] = owner_url
        text = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        if len(text) > MAX_RESULT_CHARS:
            text = json.dumps(too_large(result, len(text), page_size), separators=(",", ":"))
        return CallToolResult(content=[TextContent(type="text", text=text)])

    def too_large(result, size, page_size):
        items = next((v for v in result.values() if isinstance(v, list)), [])
        answer = {"ok": False, "error_code": "result_too_large", "next_action": "ask_for_less",
                  "result_chars": size, "limit_chars": MAX_RESULT_CHARS, "items_in_answer": len(items),
                  "detail": "Ask for less: a smaller page_size, then page_token for more; a shorter "
                            "time range; fields to keep only what you need; or a roll-up."}
        if page_size is not None and len(items) > page_size:
            # A smaller page_size won't help a type Google doesn't page by it.
            answer["detail"] = (f"Google answered {len(items)} points for a page_size of {page_size}, so "
                                "page_size doesn't limit this type: narrow the filter's time range, use "
                                "fields, or a roll-up.")
        elif len(items) > 1:
            answer["page_size_that_fits"] = max(1, len(items) * MAX_RESULT_CHARS * 9 // (size * 10))
        return answer

    def readable(data_type):
        if data_type in OUTSIDE_SCOPES:
            raise Failure("not_readable", f"{data_type} needs the googlehealth.{OUTSIDE_SCOPES[data_type]}"
                          ".readonly scope, which this connector doesn't ask Google for.")

    def data_type_path(data_type):
        return f"users/me/dataTypes/{data_type}/dataPoints"

    def family(name):
        return name and "users/me/dataSourceFamilies/" + name

    def query(data_type, filter, page_size, page_token, data_source_family, fields):
        readable(data_type)
        return {"filter": filter, "pageSize": page_size, "pageToken": page_token,
                "dataSourceFamily": family(data_source_family), "fields": fields}

    def listed(answer, items):
        """Google's JSON leaves out an empty list; say it, so an empty answer reads as one."""
        return {items: [], **answer}

    def roll_up(path, data_type, body, fields):
        readable(data_type)
        body = {k: v for k, v in body.items() if v is not None}
        return listed(health.call("POST", data_type_path(data_type) + path, {"fields": fields}, body),
                      "rollupDataPoints")

    @server.tool(annotations=read, meta=security)
    def describe_data_type(data_type: DataType | None = None) -> CallToolResult:
        """What the Google Health API holds and how to ask for it, from Google's own
        discovery document. Call it before reading a data type for the first time.

        Without data_type: every data type, with its kind (interval, sample, session, daily,
        or roll-up only), plus general notes. With data_type: which tools read it (null for a
        tool that doesn't), each answer's point schema with a working fields mask, its schema
        and every schema it refers to, the filter syntax, and notes on reading it right.

        owner_url is this deployment's own dashboard, where Google Health is connected. Give
        the owner that exact clickable link when they ask where to manage the connector.
        """
        def work():
            document = health.discovery()
            types = catalog(document)
            if data_type is None:
                return {"data_types": {name: entry.get("kind") for name, entry in sorted(types.items())
                                       if name not in OUTSIDE_SCOPES},
                        "not_readable": {name: f"needs googlehealth.{area}.readonly, which this connector "
                                         "doesn't ask for" for name, area in sorted(OUTSIDE_SCOPES.items())},
                        "notes": GENERAL_NOTES, "api_revision": document.get("revision"),
                        "owner_url": owner_url, "connector": connector}
            if data_type not in types:
                raise Failure("unknown_data_type", "Data types: " + ", ".join(sorted(types)))
            readable(data_type)
            entry, schemas = types[data_type], document["schemas"]
            field = entry.get("field")
            names = [entry["schema"]] if "schema" in entry else []
            names += [entry[a]["schema"] for a in ("roll_up", "daily_roll_up") if a in entry]
            found = referenced(schemas, names + ["DataSource"])
            reads = {"list_data_points": None, "reconcile_data_points": None,
                     "roll_up_data_points": None, "daily_roll_up_data_points": None}
            if "schema" in entry:
                found["DataPoint"] = envelope(schemas["DataPoint"], field)
                reads["list_data_points"] = {"point": "DataPoint", "id_field": "name",
                                             "fields_example": f"dataPoints(name,{field}),nextPageToken"}
            if entry.get("reconcile"):
                found["ReconciledDataPoint"] = envelope(schemas["ReconciledDataPoint"], field)
                reads["reconcile_data_points"] = {
                    "point": "ReconciledDataPoint", "id_field": "dataPointName",
                    "fields_example": f"dataPoints(dataPointName,{field}),nextPageToken"}
            for answer, tool in (("roll_up", "roll_up_data_points"), ("daily_roll_up", "daily_roll_up_data_points")):
                if answer in entry:
                    rolled = entry[answer]["field"]
                    reads[tool] = {**entry[answer], "fields_example": f"rollupDataPoints(startTime,endTime,{rolled}),nextPageToken"
                                   if answer == "roll_up" else f"rollupDataPoints(civilStartTime,civilEndTime,{rolled}),nextPageToken"}
            for tool in REFUSED.get(data_type, ()):
                reads[tool] = None
            listing = method(document, "users", "dataTypes", "dataPoints", "list")["parameters"]
            return {"data_type": data_type, "kind": entry.get("kind"),
                    "data_point_field": field, "data_point_schema": entry.get("schema"),
                    "filter_name": data_type.replace("-", "_"), "tools": reads,
                    "ignores_page_size": data_type in UNPAGED,
                    "notes": NOTES.get(data_type, []) + GENERAL_NOTES,
                    "filter_syntax": listing["filter"]["description"], "schemas": found}
        return run(work)

    @server.tool(annotations=read, meta=security)
    def list_data_points(data_type: DataType, filter: Filter | None = None, page_size: PageSize | None = None,
                         page_token: PageToken | None = None,
                         data_source_family: SourceFamily | None = None,
                         fields: Fields | None = None) -> CallToolResult:
        """List a data type's points as stored, from every source, newest first.

        Google's ListDataPoints, unchanged. Each point says its dataSource, and has an ID,
        name, where its type has one. Two devices that record the same thing give two points:
        for sleep, and anything else more than one device records, prefer
        reconcile_data_points. Continue with nextPageToken as page_token.
        """
        return run(lambda: listed(health.call("GET", data_type_path(data_type), query(
            data_type, filter, page_size, page_token, data_source_family, fields)), "dataPoints"), fields, page_size)

    @server.tool(annotations=read, meta=security)
    def reconcile_data_points(data_type: DataType, filter: Filter | None = None, page_size: PageSize | None = None,
                              page_token: PageToken | None = None,
                              data_source_family: SourceFamily | None = None,
                              fields: Fields | None = None) -> CallToolResult:
        """List a data type's points with overlapping ones from different sources merged
        into one, as the Google Health app shows them.

        Google's ReconcileDataPoints, unchanged. Its points differ from list_data_points':
        no dataSource, and an ID, where the type has one, is dataPointName. A page can be
        empty and still have a nextPageToken: continue with it as page_token.
        """
        return run(lambda: listed(health.call("GET", data_type_path(data_type) + ":reconcile", query(
            data_type, filter, page_size, page_token, data_source_family, fields)), "dataPoints"), fields, page_size)

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
            "pageSize": page_size, "pageToken": page_token}, fields), fields)

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
            "pageToken": page_token}, fields), fields)

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
        return run(work, fields)

    @server.tool(annotations=read, meta=security)
    def list_paired_devices(page_size: Annotated[int, Field(ge=1, le=100)] = 50,
                            page_token: PageToken | None = None) -> CallToolResult:
        """The trackers and watches paired with the owner's account, with their battery and
        last sync: where the data comes from, and whether it is recent.

        Google's ListPairedDevices, unchanged.
        """
        return run(lambda: listed(health.call("GET", "users/me/pairedDevices",
                                              {"pageSize": page_size, "pageToken": page_token}), "pairedDevices"))

    return {"describe_data_type": describe_data_type, "reconcile_data_points": reconcile_data_points,
            "get_resource": get_resource, "list_paired_devices": list_paired_devices}
