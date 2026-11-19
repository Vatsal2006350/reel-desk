# Reel Desk

A self-hosted dashboard for keyword-triggered Instagram Reel comment replies. Run it locally for development or deploy the webhook on AWS Lambda so it can receive Meta events while your computer is offline.

## Features

- Choose a Reel, optional whole-word keywords, and private reply in a dashboard
- Receive signed Meta comment webhooks
- Send one private reply per matching comment and avoid duplicate sends
- Optional public comment reply after the private reply succeeds
- Store hosted rules and event records in DynamoDB; load secrets from SSM Parameter Store
- Local SQLite mode for development

## Requirements

- Python 3.12+
- For local development: a Meta app, Instagram professional account, and credentials with the required Instagram messaging permissions
- For AWS deployment: AWS SAM CLI, an AWS account, and SSM SecureString parameters

## Local development

1. Create a Meta app and configure an Instagram professional account. Add the required comment webhook and messaging permissions; Meta review may be required for users outside app roles.
2. Set server-side environment variables. Never put credentials in frontend files or commit them:

```sh
export IG_USER_ID='YOUR_INSTAGRAM_PROFESSIONAL_ACCOUNT_ID'
export IG_ACCESS_TOKEN='YOUR_META_ACCESS_TOKEN'
export IG_APP_SECRET='YOUR_META_APP_SECRET'
export IG_WEBHOOK_VERIFY_TOKEN='A_LONG_RANDOM_VERIFY_VALUE'
export PUBLIC_BASE_URL='https://YOUR_PUBLIC_HTTPS_HOST'
export DASHBOARD_USER='YOUR_DASHBOARD_USERNAME'
export DASHBOARD_PASSWORD='A_LONG_RANDOM_PASSWORD'
export HOST='127.0.0.1'
python3 server.py
```

Open `http://127.0.0.1:8766`. Localhost is suitable for development only; Meta needs a public HTTPS callback to deliver real events.

## AWS deployment

`template.yaml` defines a Lambda Function URL, DynamoDB table, execution role, and log group. Before deploying, set your own account ID, Function URL, and SSM parameter names. Store the access token, app secret, webhook verification value, and dashboard password as SSM SecureString parameters. Do not deploy the sample template unchanged; replace every `YOUR_*` value and review the IAM permissions and public Function URL access.

Set the Meta webhook callback URL to your Function URL root, subscribe to the `comments` field, verify the callback, and subscribe your professional account to the app. Protect dashboard API routes with a strong password. The webhook route stays public so Meta can call it; requests are signature-checked.

The template is a starting point, not a turnkey production deployment. Confirm Meta permissions, app review, AWS region, secret parameter policies, logging, and privacy requirements for your deployment.

## Configure a Reel rule

In the dashboard, enable a rule for a Reel and write the private reply. Enter whole-word keywords separated by commas to filter comments, or leave the keyword field empty to match every comment on that Reel. Add an optional public acknowledgment; it is posted only after the private reply request succeeds. The service only sends the first private reply permitted by Meta for a matching comment. Failed or uncertain sends are recorded and not retried to avoid duplicate messages. A recipient may need to interact before a normal conversation can continue.

## Data and privacy

The service processes comment text, comment and media IDs, and event times for matching rules. Hosted deployments store rule and event data in DynamoDB, with event records configured to expire after 30 days. Local development data is written under `.work/comment-dm/`. Review `privacy.html` and adapt it to your own operator, contact, providers, retention, and deployment before using it publicly; it is a template, not legal advice.

## Safety notes

- Use only with an account and Meta app you control or are authorized to administer.
- Follow Meta's current messaging rules, rate limits, and user eligibility requirements.
- Keep tokens, app secrets, verify values, and dashboard passwords out of source control and logs.
- The service does not replay old comments. It only handles events Meta delivers after the webhook is configured.
- Do not use it for unsolicited bulk messaging.

## License

MIT. See [LICENSE](LICENSE).
