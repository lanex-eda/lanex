// vhdlmode.js — VHDL highlighting for the RTL IDE's editor, as a
// CodeMirror stream mode (the bundled legacy-modes set has none). Case
// insensitive keywords, -- and /* */ comments, strings, 'x' character
// literals, based and bit-string literals, attributes ('event, 'range) and
// the standard types. The same token names as the Verilog mode, so the
// editor's highlight style colours both alike.

const KEYWORDS = new Set((
  "abs access after alias all and architecture array assert assume attribute begin block body buffer bus case " +
  "component configuration constant context cover default disconnect downto else elsif end entity exit file for " +
  "force function generate generic group guarded if impure in inertial inout is label library linkage literal loop " +
  "map mod nand new next nor not null of on open or others out package parameter port postponed procedure process " +
  "protected pure range record register reject release rem report restrict return rol ror select severity shared " +
  "signal sla sll sra srl subtype then to transport type unaffected units until use variable wait when while with xnor xor"
).split(" "));
const TYPES = new Set((
  "std_logic std_ulogic std_logic_vector std_ulogic_vector unsigned signed integer natural positive boolean bit " +
  "bit_vector character string time real line text severity_level ieee std work numeric_std std_logic_1164 textio"
).split(" "));
const BUILTINS = new Set(("rising_edge falling_edge to_unsigned to_signed to_integer resize shift_left shift_right " +
  "now finish stop write writeline read readline to_string").split(" "));

export const vhdl = {
  name: "vhdl",
  startState: () => ({ inComment: false }),
  token(stream, state) {
    if (state.inComment) {
      if (stream.skipTo("*/")) { stream.next(); stream.next(); state.inComment = false; }
      else stream.skipToEnd();
      return "comment";
    }
    if (stream.eatSpace()) return null;
    if (stream.match("--")) { stream.skipToEnd(); return "comment"; }
    if (stream.match("/*")) { state.inComment = true; return "comment"; }
    if (stream.match(/^"(?:[^"]|"")*"/)) return "string";
    // bit strings: x"FF", b"0101", 8x"1F"
    if (stream.match(/^\d*[bBoOxXdD]"[0-9a-fA-F_xXzZuU-]*"/)) return "number";
    if (stream.match(/^'.'/)) return "string";
    if (stream.match(/^\d[\d_]*(?:#[\w.]+#)?(?:\.[\d_]+)?(?:[eE][+-]?\d+)?/)) return "number";
    if (stream.match(/^'[A-Za-z]\w*/)) return "meta";            // 'event, 'range
    if (stream.match(/^[A-Za-z][\w]*/)) {
      const w = stream.current().toLowerCase();
      if (KEYWORDS.has(w)) return "keyword";
      if (TYPES.has(w)) return "typeName";
      if (BUILTINS.has(w)) return "meta";
      return "variable";
    }
    if (stream.match(/^(<=|:=|=>|\/=|>=)/)) return "operator";
    const ch = stream.next();
    if ("()[]".includes(ch)) return "bracket";
    return null;
  },
  languageData: { commentTokens: { line: "--", block: { open: "/*", close: "*/" } } },
};
