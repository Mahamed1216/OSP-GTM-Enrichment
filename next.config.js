/**
 * The Python API (api/index.py) serves /health and /api/* — Next.js only owns
 * the UI. These rewrites hand those paths to it:
 *
 *   production  -> /api/index, the Vercel Python serverless function
 *   development -> a local `uvicorn api.index:app --port 8000`
 *
 * Array form = "afterFiles": the filesystem (pages, public/) is checked first,
 * so no UI route is shadowed.
 */
const isDev = process.env.NODE_ENV === "development";
const PY_DEV_ORIGIN = process.env.PY_API_ORIGIN || "http://127.0.0.1:8000";

/** @type {import('next').NextConfig} */
module.exports = {
  reactStrictMode: true,
  rewrites: async () => [
    {
      source: "/api/:path*",
      destination: isDev ? `${PY_DEV_ORIGIN}/api/:path*` : "/api/index",
    },
    {
      source: "/health",
      destination: isDev ? `${PY_DEV_ORIGIN}/health` : "/api/index",
    },
  ],
};
