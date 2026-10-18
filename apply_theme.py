"""Apply the new ResourceHub look to web_app.py in one step.

Put this file, resourcehub_theme.css and landing_section.html in the same folder
as web_app.py, then run:  python3 apply_theme.py

A backup of your current file is saved as web_app.py.bak first. Running the
script twice is safe.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "web_app.py")

COLOR_LINE = "color=settings.brand_color||'#536dfe';"
ON_COLOR_JS = (
    "const rgb=[1,3,5].map(i=>parseInt(color.slice(i,i+2),16)),"
    "onColor=(rgb[0]*299+rgb[1]*587+rgb[2]*114)/1000>150?'#16212b':'#ffffff';"
    "document.documentElement.style.setProperty('--on-blue',onColor);"
)


def fail(message):
    print("Nothing was changed. " + message)
    sys.exit(1)


def read(name):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        fail(f"Could not find {name} in {HERE}. Put all three files next to web_app.py.")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main():
    css = read("resourcehub_theme.css").strip("\n")
    landing = read("landing_section.html").strip()
    if not os.path.isfile(TARGET):
        fail(f"Could not find web_app.py in {HERE}.")
    with open(TARGET, encoding="utf-8") as handle:
        source = handle.read()

    style_open = source.find("<style>")
    style_close = source.find("</style>")
    if style_open == -1 or style_close == -1 or style_close < style_open:
        fail("Could not find the <style> block in web_app.py.")

    landing_start = source.find('<section class="landing-screen" id="landingScreen">')
    auth_start = source.find('<section class="auth-screen"')
    if landing_start == -1 or auth_start == -1 or auth_start < landing_start:
        fail("Could not find the landing page section in web_app.py.")
    if landing_start < style_close:
        fail("The page sections are in an unexpected order.")

    if not os.path.exists(TARGET + ".bak"):  # keep the very first backup
        shutil.copy2(TARGET, TARGET + ".bak")

    # Replace the later block first so the earlier positions stay valid.
    patched = source[:landing_start] + landing + "\n  " + source[auth_start:]
    patched = patched[:style_open + len("<style>")] + "\n" + css + "\n  " + patched[style_close:]

    note = ""
    if "'--on-blue'" not in patched:
        if COLOR_LINE in patched:
            patched = patched.replace(COLOR_LINE, COLOR_LINE + ON_COLOR_JS, 1)
            note = " Button text now adapts to light brand colors."
        else:
            note = " (Skipped the light-brand-color fix: that line was not found.)"

    with open(TARGET, "w", encoding="utf-8") as handle:
        handle.write(patched)
    print("Done. New styles and landing page applied." + note)
    print("Your previous file is saved as web_app.py.bak.")
    print("Now run:  python3 web_app.py   and open http://127.0.0.1:8000")


if __name__ == "__main__":
    main()
