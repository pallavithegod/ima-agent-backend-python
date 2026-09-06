import os

from flask import Flask, jsonify

from storage import export_notes, list_notes

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.get("/")
def index():
    items = "".join(f"<li>{note['title']}</li>" for note in list_notes())
    return f"""<!doctype html>
    <html><head><title>Flaky Notes</title></head>
    <body style="font-family: sans-serif; max-width: 40rem; margin: 3rem auto;">
      <h1>Flaky Notes</h1>
      <p>A demo notes service monitored by RecallOps.</p>
      <ul>{items}</ul>
      <form action="/notes/export" method="get">
        <button style="padding: 0.6rem 1.4rem; font-size: 1rem;">Export notes (crashes the app)</button>
      </form>
    </body></html>"""


@app.get("/notes/export")
def notes_export():
    try:
        return jsonify(export_notes())
    except Exception as error:
        # Export corruption is unrecoverable for this worker; crash so the
        # supervisor can restart it (and RecallOps can diagnose the incident).
        # os._exit is used because the dev server runs handlers in threads,
        # where SystemExit would not terminate the process.
        print(f"FATAL: notes export failed: {error!r}", flush=True)
        os._exit(1)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
