// schworker.js — runs the schematic layout (schlayout.js) off the page's
// thread, so a big module never freezes the editor. One message in (the
// graph), one out (the layout or an error).
import { layoutGraph } from "./schlayout.js";

self.onmessage = (e) => {
  const { id, graph } = e.data || {};
  try {
    self.postMessage({ id, layout: layoutGraph(graph) });
  } catch (ex) {
    self.postMessage({ id, error: String((ex && ex.message) || ex) });
  }
};
