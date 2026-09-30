"""The log analyzer service.

    api/        HTTP routes: POST, GET and DELETE /api/v1/analyses, and health
    models/     the AnalysisResult every part of the service shares
    services/   the parser and analyzer (plain Python, no web framework), and
                the streaming upload reader that feeds them
    storage/    where summaries are kept for an hour
    utils/      errors, logging, metrics, middleware, rate limiting, API keys
    config.py   every limit, overridable by environment variable
    main.py     builds the FastAPI app; run with `uvicorn app.main:app`
"""
