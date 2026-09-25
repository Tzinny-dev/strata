import { withMermaid } from "vitepress-plugin-mermaid";

export default withMermaid({
  title: "Strata",
  description: "Declarative, versioned, immutable data transformations — compiles to SQL",
  base: "/",
  ignoreDeadLinks: true,
  head: [["link", { rel: "icon", type: "image/svg+xml", href: "/logo.svg" }]],
  markdown: {
    // strata code blocks fallback to txt (no custom grammar yet)
  },
  themeConfig: {
    outline: "deep",
    search: { provider: "local" },
    lastUpdated: true,
    editLink: {
      pattern: "https://github.com/Tzinny-dev/strata/edit/main/:path",
      text: "Edit this page on GitHub"
    },
    nav: [
      { text: "Guide", link: "/guide/getting-started" },
      { text: "Reference", link: "/reference/syntax-reference" },
      { text: "Spec", link: "/spec/grammar" },
      { text: "Changelog", link: "/changelog" },
      { text: "GitHub", link: "https://github.com/Tzinny-dev/strata" },
      { text: "PyPI", link: "https://pypi.org/project/strata-lang/" }
    ],
    sidebar: {
      "/guide/": [
        { text: "Getting Started", link: "/guide/getting-started" },
        { text: "Tutorial", link: "/guide/tutorial" },
        { text: "Warehouse Semantics", link: "/guide/warehouse-semantics" },
        { text: "Warehouse Adapters", link: "/guide/warehouse-adapters" },
        { text: "VS Code", link: "/guide/vscode" },
        { text: "Binary Standalone", link: "/guide/binary-standalone" }
      ],
      "/reference/": [
        { text: "Syntax", link: "/reference/syntax-reference" },
        { text: "Strict Contracts", link: "/reference/strict-contracts" },
        { text: "Incremental", link: "/reference/incremental" },
        { text: "SetOps & Dedup", link: "/reference/setops" },
        { text: "JSON / Arrays", link: "/reference/json-arrays" },
        { text: "Date Functions", link: "/reference/date-functions" },
        { text: "Join Cardinality", link: "/reference/join-cardinality" },
        { text: "Nested Domains", link: "/reference/nested-domains" }
      ],
      "/spec/": [
        { text: "Grammar", link: "/spec/grammar" },
        { text: "Types & Contracts", link: "/spec/types-and-contracts" }
      ]
    },
    socialLinks: [
      { icon: "github", link: "https://github.com/Tzinny-dev/strata" }
    ],
    footer: {
      message: "Released under the MIT License.",
      copyright: "Copyright © 2026 Tzinny-dev / Strata"
    }
  }
})
