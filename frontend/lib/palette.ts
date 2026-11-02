// Reference data-viz palette (validated categorical order; status colors reserved for states).
// Status colors are never used alone: every use is paired with a text label or legend.
export const SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]; // first three slots validate all-pairs
export const STATUS = { good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b", neutral: "#a9a8a2" };
export const INK = { primary: "#0b0b0b", secondary: "#52514e", muted: "#7c7b76", grid: "#e4e3df", surface: "#fcfcfb" };

export const EVIDENCE_COLOR: Record<string, string> = {
  positive: STATUS.good, negative: STATUS.critical, absent: STATUS.serious, unknown: STATUS.neutral,
};
export const EVIDENCE_LABEL: Record<string, string> = {
  positive: "Positive evidence", negative: "Negative evidence", absent: "Absent (no matching record)", unknown: "Unknown (no data)",
};
export const CLASS_COLOR: Record<string, string> = {
  eligible: STATUS.good, potentially_eligible: STATUS.warning, ineligible: STATUS.neutral,
};
export const CLASS_LABEL: Record<string, string> = {
  eligible: "Eligible", potentially_eligible: "Potentially eligible", ineligible: "Ineligible",
};
