/** @type {import('next').NextConfig} */
const nextConfig = {
  typedRoutes: false,
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async redirects() {
    return [
      { source: "/employee", destination: "/legacy-flow", permanent: false },
      { source: "/assistant", destination: "/?panel=sop", permanent: false },
      { source: "/studio", destination: "/?panel=publish", permanent: false },
      { source: "/approval", destination: "/legacy-flow?panel=review", permanent: false },
      { source: "/tasks", destination: "/legacy-flow?panel=execution", permanent: false },
      { source: "/profiles", destination: "/accounts?panel=profile", permanent: false },
      { source: "/strategies", destination: "/accounts?panel=strategy", permanent: false },
      { source: "/knowledge", destination: "/accounts?panel=knowledge", permanent: false },
      { source: "/settings", destination: "/accounts?panel=settings", permanent: false },
    ];
  },
};

export default nextConfig;
