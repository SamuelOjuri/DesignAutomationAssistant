/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async headers() {
    return [{
      source: "/specification-review/:path*",
      headers: [
        {
          key: "Content-Security-Policy",
          value: "frame-ancestors 'self' https://monday.com https://*.monday.com; frame-src 'self' blob:; object-src 'none'; base-uri 'self'",
        },
        { key: "Referrer-Policy", value: "no-referrer" },
        { key: "Cache-Control", value: "private, no-store" },
        { key: "X-Content-Type-Options", value: "nosniff" },
      ],
    }];
  },
};

export default nextConfig;
