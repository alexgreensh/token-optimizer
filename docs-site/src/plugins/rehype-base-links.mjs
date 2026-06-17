// @ts-check
/**
 * rehype-base-links
 *
 * Starlight prepends the configured `base` to sidebar/nav links automatically,
 * but it does NOT touch root-absolute links written inline in Markdown/MDX prose
 * (e.g. `[first audit](/start/first-audit/)`). When the site deploys under a base
 * path — the GitHub Pages project site `https://alexgreensh.github.io/token-optimizer/`
 * — those inline links resolve against the domain root (`/start/first-audit/`) and 404.
 *
 * This plugin rewrites root-absolute internal `href`/`src` values at build time to
 * include the base. It is a no-op when `base` is empty ("" — a custom root domain),
 * so it survives the documented one-line domain switch in astro.config.mjs without
 * any per-link changes.
 *
 * Left untouched:
 *   - external / protocol-relative links (`http://…`, `//cdn…`)
 *   - in-page anchors and query-only links (`#section`, `?q=1`)
 *   - non-absolute links (`./foo`, `foo/bar`)
 *   - links already carrying the base prefix (idempotent)
 *
 * @param {{ base?: string }} [options]
 */
export default function rehypeBaseLinks(options = {}) {
  // Normalize the base to no trailing slash, e.g. "/token-optimizer".
  const base = (options.base || "").replace(/\/+$/, "");

  /** @param {string} value */
  const rewrite = (value) => {
    if (typeof value !== "string") return value;
    // Only root-absolute, internal paths: start with a single "/".
    if (!value.startsWith("/") || value.startsWith("//")) return value;
    if (!base) return value; // root domain: nothing to prepend.
    // Already prefixed (exact base, or base followed by "/", "#", "?").
    if (value === base || value.startsWith(base + "/") ||
        value.startsWith(base + "#") || value.startsWith(base + "?")) {
      return value;
    }
    return base + value;
  };

  // Attribute to rewrite per element tag.
  const attrByTag = { a: "href", area: "href", img: "src", source: "src" };

  return (tree) => {
    /** @param {any} node */
    const walk = (node) => {
      if (node && node.type === "element") {
        const attr = attrByTag[node.tagName];
        if (attr && node.properties && typeof node.properties[attr] === "string") {
          node.properties[attr] = rewrite(node.properties[attr]);
        }
      }
      if (node && Array.isArray(node.children)) {
        for (const child of node.children) walk(child);
      }
    };
    walk(tree);
    return tree;
  };
}
