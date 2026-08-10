// Renders the ```mermaid fences produced by pymdownx.superfences.
//
// If mermaid fails to load (offline build, blocked CDN), nothing runs and the
// reader sees the diagram's source text rather than a broken page.
if (window.mermaid) {
  window.mermaid.initialize({
    startOnLoad: true,
    theme: "neutral",
    securityLevel: "strict",
    flowchart: { curve: "basis", useMaxWidth: true },
  });
}
