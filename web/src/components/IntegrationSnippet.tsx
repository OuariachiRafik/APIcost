/**
 * The payoff screen of onboarding (UC-05).
 *
 * The entire product promise is "change one config value", so this shows
 * exactly that one line changing, in the three forms a solo developer actually
 * uses. The proxy key appears here and nowhere else in the UI — once the user
 * navigates away it is unrecoverable, which is why the warning is loud.
 */
import { CodeBlock } from './ui';

const PROXY_BASE_URL = import.meta.env.VITE_PROXY_BASE_URL ?? 'http://localhost:8000/v1';

export function IntegrationSnippet({ proxyKey }: { proxyKey: string }) {
  const python = `from openai import OpenAI

client = OpenAI(
    base_url="${PROXY_BASE_URL}",   # <- the only line that changes
    api_key="${proxyKey}",
)

client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hi"}],
)`;

  const node = `import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "${PROXY_BASE_URL}",   // <- the only line that changes
  apiKey: "${proxyKey}",
});

await client.chat.completions.create({
  model: "gpt-4o",
  messages: [{ role: "user", content: "hi" }],
});`;

  const curl = `curl ${PROXY_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer ${proxyKey}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'`;

  return (
    <div className="space-y-4">
      <div className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
        <strong>Copy this key now.</strong> It is shown once and stored only as a hash — we cannot
        show it to you again. If you lose it, issue a new one and revoke this one.
      </div>

      <CodeBlock label="Your proxy key" code={proxyKey} />
      <CodeBlock label="Python (openai SDK)" code={python} />
      <CodeBlock label="Node (openai SDK)" code={node} />
      <CodeBlock label="cURL" code={curl} />

      <p className="text-sm text-slate-600">
        Your existing code keeps working unchanged — same request shape, same response shape.
        APICost adds its own metadata in response headers, never in the body.
      </p>
    </div>
  );
}
