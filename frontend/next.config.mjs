/** API requests are proxied to the FastAPI backend so the browser talks to one origin. */
const API = process.env.CLINIQ_API_URL || "http://localhost:8000";
export default {
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};
