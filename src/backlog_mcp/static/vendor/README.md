- marked 18.0.14: https://registry.npmjs.org/marked/-/marked-18.0.14.tgz
- dompurify 3.4.15: https://registry.npmjs.org/dompurify/-/dompurify-3.4.15.tgz
- highlight.js 11.12.0: https://registry.npmjs.org/@highlightjs/cdn-assets/-/cdn-assets-11.12.0.tgz
- mermaid 12.0.0: https://registry.npmjs.org/mermaid/-/mermaid-12.0.0.tgz

Use the published browser files without a build step: `highlight.min.js` from
the Highlight.js CDN package, and `dist/mermaid.min.js` from Mermaid.
The Highlight.js common-language bundle includes Python, JavaScript, TypeScript,
Rust, Go, shell, SQL, JSON, YAML, HTML, CSS, and other common languages.
`highlight-light.css` and `highlight-dark.css` are the published GitHub themes.
JavaScript bundles are tracked with Git LFS; fetch them before running or packaging.
Mermaid loads only when a diagram is rendered. No CDN requests are made at runtime.
