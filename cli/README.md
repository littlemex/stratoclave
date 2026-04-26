# Stratoclave CLI

A CLI tool that connects to the Stratoclave API using Cognito authentication.

*Last updated: 2026-04-16*

## Build

```bash
cargo build --release
```

## Configuration

Create `~/.stratoclave/config.toml`:

```toml
[auth]
client_id = "<YOUR_CLIENT_ID>"
cognito_domain = "https://<YOUR_COGNITO_DOMAIN>.auth.us-east-1.amazoncognito.com"
redirect_uri = "http://localhost:18080/callback"

[api]
endpoint = "https://<YOUR_API_ENDPOINT>"
```

## Usage

### Basic Usage

```bash
echo "hello" | ./target/release/stratoclave
```

### Authentication Flow

1. When the CLI is launched, the Cognito login page opens automatically in the browser
2. Enter your email and password to log in
3. After successful authentication, the CLI obtains an ID token (used for backend OIDC verification which requires the `aud` claim)
4. A request is sent to the Stratoclave API
5. The API response is displayed

### If the authentication page does not open

If the browser does not open automatically, manually navigate to the following URL:

```
https://<YOUR_COGNITO_DOMAIN>.auth.us-east-1.amazoncognito.com/oauth2/authorize?client_id=<YOUR_CLIENT_ID>&response_type=code&scope=openid+email+profile&redirect_uri=http://localhost:18080/callback
```

## Troubleshooting

### Port 18080 is in use

The CLI will not work if another program is using port 18080. Check the port with:

```bash
lsof -i :18080
```

### Authentication fails

- Verify that your email and password are correct
- Confirm that the user is enabled in the User Pool

```bash
aws cognito-idp admin-get-user \
  --user-pool-id <YOUR_USER_POOL_ID> \
  --username your-email@example.com \
  --region us-east-1
```

### API errors

Check that the Stratoclave Backend is running properly:

```bash
curl https://<YOUR_API_ENDPOINT>/health
```

## Cognito Configuration

- User Pool ID: `<YOUR_USER_POOL_ID>`
- Client ID: `<YOUR_CLIENT_ID>`
- OIDC Issuer: `https://cognito-idp.us-east-1.amazonaws.com/<YOUR_USER_POOL_ID>`
- Cognito Domain: `https://<YOUR_COGNITO_DOMAIN>.auth.us-east-1.amazoncognito.com`
- Redirect URI: `http://localhost:18080/callback`

## API Endpoints

- Backend: `https://<YOUR_API_ENDPOINT>`
- Frontend: `https://<YOUR_FRONTEND_URL>`
- ACP Endpoint: `https://<YOUR_API_ENDPOINT>/api/acp`
