# msisdn-identity-service

A minimal Matrix Identity Service that only implements the two `msisdn`
(phone number) validation endpoints, backed by SMSPoh instead of Twilio.
Synapse talks to this directly — ma1sd is not involved for SMS at all.

## 1. Configure

```
cp .env.example .env
# fill in SMSPOH_API_KEY, SMSPOH_API_SECRET, SMSPOH_SENDER_ID, BRAND_NAME
```

`SMSPOH_SENDER_ID` must be a sender name/number approved on your SMSPoh
account. `BRAND_NAME` gets embedded in the OTP text — SMSPoh rejects OTP
messages that don't contain a brand/app name in the body.

## 2. Add to your docker-compose.yml

```yaml
  msisdn-identity:
    build: ./msisdn-service
    container_name: msisdn-identity
    restart: unless-stopped
    env_file:
      - ./msisdn-service/.env
    # Do NOT publish this port to the host / internet.
    # Only Synapse needs to reach it, over the internal compose network.
    expose:
      - "8091"
```

Then add `depends_on: [msisdn-identity]` to the `synapse` service (or just
make sure it's up before Synapse starts making requests).

## 3. Point Synapse at it — homeserver.yaml

```yaml
account_threepid_delegates:
  msisdn: http://msisdn-identity:8091
  # email: http://ma1sd:8090   # keep ma1sd for email if you're still using it
```

Note: no `https://` needed since this is internal container-to-container
traffic. Restart Synapse after adding this.

## 4. Test it

From inside the synapse container (or any container on the same network):

```bash
curl -X POST http://msisdn-identity:8091/_matrix/identity/v2/validate/msisdn/requestToken \
  -H 'Content-Type: application/json' \
  -d '{"client_secret":"test123","country":"MM","phone_number":"09xxxxxxxxx","send_attempt":1}'
```

You should get `{"sid": "...", "msisdn": "959xxxxxxxxx"}` back and receive
an SMS. Then in Element (or via the client-server API), adding/verifying a
phone number on an account will trigger this flow automatically through
Synapse.

## Notes / things to adjust for production

- **Session storage is in-memory** (a Python dict). Fine for a single
  container instance. If you ever scale this to multiple replicas, switch
  `_sessions` / `_send_history` to Redis, since state needs to be shared.
- **Rate limiting is basic** — 5 SMS/hour per number by default
  (`MAX_SEND_PER_HOUR`), 5 wrong-OTP attempts before a session is killed
  (`MAX_SUBMIT_ATTEMPTS`). Tune these via env vars.
- **No auth on the endpoints** — this mirrors how Synapse's
  `account_threepid_delegates` mechanism works (it doesn't send any
  credential). The only protection is network isolation — keep this
  service off any publicly exposed port, since anyone who can reach it
  can make you send arbitrary SMS via your SMSPoh balance.
- Only the `msisdn` medium is implemented — this service does not do
  `lookup`, `bind`, directory search, or anything else ma1sd does. Keep
  ma1sd around for those if you need them.
