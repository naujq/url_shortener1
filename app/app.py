import os
import string
import secrets
from functools import wraps
from urllib.parse import urlparse
 
from flask import Flask, request, redirect, jsonify
import redis
import psycopg2
 
app = Flask(__name__)
 
# Define the 62 characters used for short codes (0-9, a-z, A-Z)
ALPHABET = string.digits + string.ascii_lowercase + string.ascii_uppercase
CODE_LENGTH = 7          # 3.5 trillion possible codes
MAX_CODE_ATTEMPTS = 5    # retries if a random code happens to collide
MAX_URL_LENGTH = 2048
 
# Connect to Redis for fast lookups
cache = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", 6379)),
    decode_responses=True,
)
 
 
def rate_limit(max_requests=10, window=60):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            key = f"rate_limit:{request.remote_addr}"
            try:
                count = cache.incr(key)
                if count == 1:
                    # Only set the expiry when the window starts, so it isn't extended on every request
                    cache.expire(key, window)
                if count > max_requests:
                    return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
            except redis.RedisError:
                app.logger.warning("Redis unavailable; skipping rate limit")
            return f(*args, **kwargs)
        return decorated_function
    return decorator
 
 
def get_db():
    """Connect to PostgreSQL for permanent storage."""
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "db"),
        database=os.environ.get("POSTGRES_DB", "urlshortener"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "postgres"),
    )
 
 
def generate_short_code(length=CODE_LENGTH):
    """Random, non-sequential code using a cryptographically secure RNG."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
 
 
def is_valid_short_code(code):
    return 0 < len(code) <= 16 and all(c in ALPHABET for c in code)
 
 
def validate_url(url):
    """Return an error message if the URL is invalid, otherwise None."""
    if not isinstance(url, str) or not url.strip():
        return "url must be a non-empty string"
    if len(url) > MAX_URL_LENGTH:
        return f"url must be at most {MAX_URL_LENGTH} characters"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "url must start with http:// or https://"
    if not parsed.netloc:
        return "url must include a domain"
    return None
 
 
def cache_set(short_code, long_url):
    """Caching is best-effort; a Redis outage shouldn't break the request."""
    try:
        cache.set(short_code, long_url)
    except redis.RedisError:
        app.logger.warning("Failed to cache %s", short_code)
 
 
def find_existing_code(cur, long_url):
    cur.execute("SELECT short_code FROM urls WHERE long_url = %s", (long_url,))
    row = cur.fetchone()
    return row[0] if row else None
 
 
@app.route("/shorten", methods=["POST"])
@rate_limit(max_requests=10, window=60)
def shorten_url():
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "Missing url field"}), 400
 
    long_url = data["url"].strip() if isinstance(data["url"], str) else data["url"]
    error = validate_url(long_url)
    if error:
        return jsonify({"error": error}), 400
 
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # Reuse the existing code if this URL was already shortened
            short_code = find_existing_code(cur, long_url)
            status = 200
 
            if not short_code:
                status = 201
                for _ in range(MAX_CODE_ATTEMPTS):
                    candidate = generate_short_code()
                    cur.execute(
                        """
                        INSERT INTO urls (short_code, long_url) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING short_code
                        """,
                        (candidate, long_url),
                    )
                    row = cur.fetchone()
                    if row:
                        short_code = row[0]
                        break
                    # Conflict: either the code collided, or another request
                    # inserted this same URL a moment ago. Check for the latter.
                    short_code = find_existing_code(cur, long_url)
                    if short_code:
                        status = 200
                        break
 
            if not short_code:
                conn.rollback()
                return jsonify({"error": "Could not generate a unique short code"}), 500
 
        conn.commit()
    except psycopg2.Error:
        if conn:
            conn.rollback()
        app.logger.exception("Database error while shortening URL")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()
 
    cache_set(short_code, long_url)
 
    return jsonify({
        "short_url": f"{request.host_url}{short_code}",
        "short_code": short_code,
    }), status
 
 
@app.route("/<short_code>")
def redirect_url(short_code):
    # Reject anything that couldn't be a valid code before touching Redis or Postgres
    if not is_valid_short_code(short_code):
        return jsonify({"error": "Short URL not found"}), 404
 
    try:
        long_url = cache.get(short_code)
    except redis.RedisError:
        long_url = None
 
    if long_url:
        print("CACHE HIT", flush=True)
        return redirect(long_url, code=302)
 
    print("CACHE MISS - querying database", flush=True)
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT long_url FROM urls WHERE short_code = %s", (short_code,))
            result = cur.fetchone()
    except psycopg2.Error:
        app.logger.exception("Database error during redirect")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        if conn:
            conn.close()
 
    if not result:
        return jsonify({"error": "Short URL not found"}), 404
 
    long_url = result[0]
    cache_set(short_code, long_url)
    return redirect(long_url, code=302)
 
 
@app.route("/health")
def health():
    """Endpoint to verify if the server is still running"""
    return jsonify({"status": "healthy"}), 200
 
 
@app.route("/")
def index():
    return app.send_static_file("index.html")
 
 
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)