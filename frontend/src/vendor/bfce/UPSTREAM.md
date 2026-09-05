# bfce provenance

- Requested upstream: https://github.com/bwndapp/bfce
- Canonical upstream after permanent redirect: https://github.com/bwndapp/bbot
- Approved revision: `814e199c2045b3be057f59f8dc4ed395a4d2bbd6`
- Verification: the approved revision is present in the canonical `bwndapp/bbot` history and is an ancestor of canonical HEAD `93b20803e023dda35ba13dca4b95fc450b39d00d`.
- License: MIT, preserved verbatim in `LICENSE`.
- Vendored paths: `Face.jsx`, `core.js`, `expressions.js`, `face.css`, `index.js`.
- Runtime policy: this repository uses no CDN, runtime download, or face-owned network request.
- Local hardening note: lifecycle-only changes to `core.js` ensure pointer listeners are reference-counted and removed on final destroy, reduced-motion faces do not schedule RAF work, and offscreen faces pause the shared loop. The approved upstream revision remains the source baseline.
- Local adapter boundary: `index.js` exports only the hardened `Face` component. The raw `createFace` engine and upstream expression/reaction catalogs remain internal. `Face` normalizes all initial and imperative expression changes to the approved Katherine vocabulary and does not forward arbitrary props to the host element.
