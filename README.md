# Learn2Earn Resource Desk

A browser-based resource management dashboard built with Python's standard
library. It uses the existing `main.py` inventory logic and stores changes in
`data.json` in the directory where you start the server.

## Run locally

From this project directory, run:

```sh
python3 web_app.py
```

Then open <http://127.0.0.1:8000> in your browser. Press `Ctrl+C` in the
terminal to stop the server. The classic command-line program is still
available with `python3 main.py`.

Use **Create account** on the sign-in screen to register. Usernames and
password hashes are stored in `auth.json`; passwords are never saved as plain
text. Accounts currently share the same access to the inventory.

The web server listens on the local computer only. Hosting this for a campus
or public network requires deployment configuration and account administration.
