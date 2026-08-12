# Security

The gateway is intended only for trusted LAN or private VPN use. Do not expose port 8765 to the public internet. Pairing accepts private peers, uses an expiring one-time code, returns a high-entropy bearer token, and persists only a salted PBKDF2 hash. WebSocket authentication uses its header, never a query parameter. Logs must exclude authorization headers, tokens, pairing codes, and conversation content.
