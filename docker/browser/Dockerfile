# Browser image (§6.5): Chromium on a virtual screen, driven by browserd as uid 1000.
#
# Playwright's own image, pinned to an exact build: Ubuntu 24.04 with the Chromium that
# Playwright 1.63.0 was built and tested against. When updating, change the tag, the digest
# and the playwright line in requirements-browser.in together.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

# tini reaps the processes Chromium leaves behind. xsel empties the screen's clipboard when
# Roland hands the browser back. x11vnc is the screen server for M7: installed, never started
# here. The fonts are there so that pages in any script are readable in screenshots.
# Only Chromium is used: the other browsers and the video tool in the base image are removed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini x11vnc xsel fonts-noto-core fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /ms-playwright/firefox-* /ms-playwright/webkit-* /ms-playwright/ffmpeg-* \
              /ms-playwright/chromium_headless_shell-*

WORKDIR /app
# Hash-checked, like the other images: a tampered package fails the build.
# Remake the lock with the command at the top of requirements-browser.in.
COPY requirements-browser.lock ./
RUN python3 -m pip install --no-cache-dir --break-system-packages --require-hashes -r requirements-browser.lock
COPY browserd ./browserd

# Managed policies: password manager, autofill, sync, sign-in and metrics off, no extensions,
# no local or browser-internal pages. Playwright's Chromium is "Chrome for Testing", which
# reads the second folder (checked: with the file only in the first, chrome://policy opens
# and nothing is applied). The first is where a plain Chromium build would look.
# Not in the file: DeveloperToolsAvailability=2 from the spec. It also turns off the pipe
# Playwright drives the browser through, so the browser never starts. The agent has no way
# to open developer tools anyway: no key for it, and devtools:// pages are refused.
COPY docker/browser/chromium-policy.json /etc/chromium/policies/managed/roland-agent.json
COPY docker/browser/chromium-policy.json /etc/opt/chrome_for_testing/policies/managed/roland-agent.json

# /profile holds the signed-in browser profile and is mounted into this container only.
# /files is the browser's corner of the workspace (downloads, staged uploads).
RUN mkdir -p /profile /files \
    && chown 1000:1000 /profile /files \
    && chmod 0700 /profile \
    && chmod -R a+rX /etc/chromium /etc/opt/chrome_for_testing /app
USER 1000:1000
ENV HOME=/tmp/home \
    PYTHONPATH=/app \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DISPLAY=:99 \
    BROWSERD_HOST=10.77.4.40 \
    BROWSERD_PORT=7100
ENTRYPOINT ["tini", "--"]
CMD ["python", "-m", "browserd"]
