"""Part 2: the HTTP API.

A thin FastAPI layer over the :mod:`analyzer` package: it reads an upload
without buffering it, bounds how much work runs at once, stores the summary
for an hour, and reports every failure in one shape.
"""
