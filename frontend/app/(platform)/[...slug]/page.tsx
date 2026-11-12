import { pageTitle } from "@/lib/page-title";
import type { Metadata } from "next";

export async function generateMetadata(
  props: PageProps<"/[...slug]">,
): Promise<Metadata> {
  const { slug } = await props.params;
  const title = pageTitle(`/${slug.join("/")}`);
  // Without a title of its own a screen keeps the site's default one.
  return title ? { title } : {};
}

// Rendered by the (platform) layout.
export default function Page() {
  return null;
}
