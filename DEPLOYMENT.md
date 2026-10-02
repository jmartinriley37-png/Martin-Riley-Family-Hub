# Deploy the private family server

Use one persistent Linux host with Python 3.12+, HTTPS, and a domain.
GitHub Pages/static hosting cannot run this API. No Supabase/Firebase account
or third-party auth API key is required by this implementation.

1. Place the repository on the host. The original ZIP is retained in source
   control but is not served by the application.
2. Choose a private directory owned by the service account for SQLite.
   Restrict permissions to that account, e.g. directory mode 700.
3. Set the same HUB_DB value when provisioning accounts and running the service.
   Run server.py --user Dad, --user Mom, --user Daughter interactively.
4. Configure the service environment:

   ~~~sh
   HUB_DB=/var/lib/martin-riley/hub.sqlite3
   HUB_HOST=127.0.0.1
   PORT=8080
   HUB_ORIGIN=https://family.example.com
   HUB_SECURE_COOKIE=1
   ~~~

5. Run python3 server.py as a non-root service account under a process supervisor.
   Restart on failure. Expose only HTTPS; block public port 8080.
6. Terminate TLS at a reverse proxy, reject unknown Host headers, cap request
   bodies at 32 KiB, and enforce header/request timeouts and connection limits.
   Example Caddy site:

   ~~~caddy
   family.example.com {
       reverse_proxy 127.0.0.1:8080
       header Strict-Transport-Security "max-age=31536000"
   }
   ~~~

   Behind a proxy, the built-in login limit sees the proxy address and shares its
   limit across the family. Add proxy-level per-client login rate limiting as needed;
   do not blindly trust caller-supplied forwarding headers.

7. Back up using SQLite's online backup API, not by copying only the main file
   while writes are active (WAL may contain committed data). Encrypt off-host
   backups, retain several versions, and test restoring them.
8. Verify all three sign-ins on HTTPS. Create Family, Adults Only, Assigned,
   and Me Only items; inspect Daughter's actual /api/state response. Check that
   Mom/Dad cannot read each other's Me Only items. Test task completion, approvals,
   and changes appearing on another phone within five seconds.
9. Android: Chrome → Install/Add to Home Screen.
   iPhone: Safari → Share → Add to Home Screen. Serve the app at the domain root;
   manifest and API URLs are root-relative.

Before family use, review authentication, HTTP serving, backup security, device
installation, and privacy end to end. Rotate passwords after device loss using
the account reset command; this invalidates that person's sessions. Stop the
service before operating on a restored database.

Push notifications, self-service password recovery, MFA, and independently
reviewed production security are separate follow-up work.
