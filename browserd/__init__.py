"""browserd: the private service that drives one Chromium for the agent (§6.5, §8.4).

It ships only in the browser image. Core talks to it over the internal `browser_ctl` network
with a Bearer token; nothing here is ever published to the internet.
"""
