"""Business questions answered from the portal API (spec 006).

Two rules hold this package together:

* the model never builds a URL, a route, or a free parameter — it only picks an
  intent from the versioned catalog (:mod:`os_system_agent.ask.intents`);
* every date is computed in the business timezone, never in the host's UTC
  (:mod:`os_system_agent.ask.dateparse`).

Nothing here performs I/O against the portal; the HTTP client lives in
``os_system_agent.portal``.
"""
