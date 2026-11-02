// End-to-end UI walkthrough of the required demo (headless Chromium via Playwright).
// Usage: node scripts/demo_walkthrough.mjs [baseUrl] [screenshotDir]
// Fails (exit 1) on any browser console error or failed step.
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import path from "node:path";

const BASE = process.argv[2] || "http://localhost:3000";
const OUT = process.argv[3] || "docs/screenshots";
const UPLOAD = path.resolve("data/demo/demo_upload_bundle.json");
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("pageerror", (e) => errors.push(String(e)));
const shot = async (name) => { await page.waitForTimeout(400); await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true }); console.log("screenshot", name); };

// Sign in as admin (dev role)
await page.goto(BASE);
await page.getByRole("button", { name: /Admin/ }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
await shot("01_overview");

// 1-2. Upload a synthetic FHIR dataset; validate & ingest
await page.goto(`${BASE}/pipeline`);
await page.setInputFiles('input[type="file"]', UPLOAD);
await page.getByRole("button", { name: "Upload & ingest" }).click();
await page.getByText("Upload & ingest", { exact: false }).first().waitFor();
await page.locator("text=Rejected → DLQ").first().waitFor({ timeout: 30000 });
await shot("02_pipeline_upload_ingested");

// 3. Inspect the normalized clinical database
await page.goto(`${BASE}/data`);
await page.getByPlaceholder("Search patient id…").fill("demo-0001");
await page.getByRole("cell", { name: "demo-0001" }).click();
await page.getByText("Biomarker results").nth(1).waitFor();
await page.getByRole("button", { name: /raw#/ }).nth(1).click();
await page.getByText("Source file:").waitFor();
await shot("03_data_explorer_lineage");

// 4-6. Enter a request, extract criteria, review the structured representation
await page.goto(`${BASE}/cohort`);
await page.getByRole("button", { name: "Extract criteria" }).click();
await page.getByText("Structured representation").waitFor();
await shot("04_cohort_extraction");
await page.getByRole("button", { name: "Validate & compile" }).click();
await page.getByText("Review compiled query").waitFor();
await page.getByText(/Compiled SQL/).click();
await shot("05_cohort_review_sql");

// 7-8. Approve and execute
await page.getByRole("button", { name: "Approve query" }).click();
await page.getByRole("button", { name: "Execute query" }).click();
await page.waitForURL(/\/cohort\/runs\//, { timeout: 30000 });

// 9. Visualize cohort and attrition funnel
await page.getByText("Attrition funnel").waitFor();
await shot("06_cohort_results_funnel");
await page.getByRole("button", { name: "Explain" }).first().click();
await page.getByRole("button", { name: "Reproduce run" }).click();
await page.getByText(/Reproduced|differs/).waitFor({ timeout: 30000 });
await shot("07_cohort_explanations_reproduced");

// 10-11. Retrieve supporting evidence; inspect citations and missing information
await page.getByRole("link", { name: "Supporting evidence" }).click();
await page.getByText(/criteria · \d+ with insufficient evidence/).waitFor({ timeout: 30000 });
await page.getByRole("button", { name: "Search" }).click();
await page.getByText(/passages ·/).waitFor();
await shot("08_evidence_citations");

// 12-13. Run the evaluation benchmark; inspect metrics and data quality
await page.goto(`${BASE}/evaluation`);
await page.getByRole("button", { name: "Run benchmark now" }).click();
await page.getByRole("button", { name: "Run benchmark now" }).waitFor({ timeout: 180000 });
await page.getByRole("button", { name: /Show failing items/ }).click();
await page.getByText(/Failing items/).waitFor();
await shot("09_evaluation");
await page.goto(`${BASE}/quality`);
await page.getByText("Dead-letter queue").waitFor();
await page.locator("tr", { hasText: "demo_upload_bundle.json#entry" }).first().click();
await page.getByText("Error detail · record").waitFor();
await shot("10_quality_lineage");

await browser.close();
if (errors.length) { console.error("Browser console errors:\n" + errors.join("\n")); process.exit(1); }
console.log("walkthrough OK");
