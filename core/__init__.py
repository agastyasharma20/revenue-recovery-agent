"""Core package for the AI Revenue Recovery Agent."""

# Load a local .env (if present) as soon as anything imports core.* -- this
# is what lets GROQ_API_KEY / RAZORPAY_* in .env reach classifier.py,
# run_evaluation.py, the dashboard, etc. without every entry-point script
# needing its own load_dotenv() call. No-op if .env doesn't exist, and
# never overrides a variable already set in the real environment.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv()
except ImportError:
    pass
