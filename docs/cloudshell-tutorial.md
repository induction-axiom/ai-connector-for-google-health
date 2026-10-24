# Set up your private AI connector for Google Health

<walkthrough-tutorial-duration duration="10"></walkthrough-tutorial-duration>

## What you'll do

1. Run the setup
2. Connect Google Health
3. Connect your AI app

## Step 1 of 3: Run the setup

Pick your project:

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

Paste this into the terminal (click the copy icon), press **Enter**, and type
`y` when asked. If it asks you to sign in with Google, open its link, allow,
and paste the code back.

```sh
firebase/scripts/bootstrap.sh <walkthrough-project-id/>
```

Wait a few minutes for **✓ Setup complete**. If it stops with **✗**, fix
what it says and run the same command again.

## Step 2 of 3: Connect Google Health

First sign in once to the Google Health app on your phone, with the Google
account that has your health data.

Open the **dashboard** link the terminal printed, and bookmark it. Sign in with
Google, choose **Connect Google Health**, then **Continue with Google**. Google
warns that it hasn't verified the app: it is your own connector, so continue.

## Step 3 of 3: Connect your AI app

In the dashboard: **AI apps** → your app → **How to connect**.

## You're set

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>

Ask your AI app how you've been sleeping.
