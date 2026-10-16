/**
 * Tessera on Cloudflare: the Worker in front of the Tessera container.
 *
 *  browser / Power BI / MCP client ──▶ Worker (TLS, Access, this file) ──▶ Tessera container (Python, DuckDB)
 *                                                                              │ backups every few minutes,
 *                                                                              ▼ after runs and on shutdown
 *                                                                          R2 bucket
 *
 * The Worker
 *  - routes every request to the one Tessera instance and wakes it when it sleeps,
 *  - shows a "starting" page while a fresh container restores its data from R2,
 *  - passes the visitor's real address so sign-in lockout and rate limits work per person,
 *  - keeps Tessera running on a cron so schedules, alerts and streams fire on time,
 *  - hands Tessera its settings and secrets (every TESSERA_* variable, plus the R2 endpoint).
 */
import { Container, getContainer } from "@cloudflare/containers";
import { containerEnv, forwardRequest, starting } from "./helpers";

export interface Env {
  TESSERA: DurableObjectNamespace<TesseraContainer>;
  DATA: R2Bucket;
  KEEP_WARM?: string;
  SLEEP_AFTER?: string;
  R2_ACCOUNT_ID?: string;
  [key: string]: unknown;
}

const PORT = 8710;
const INSTANCE = "tessera";

export class TesseraContainer extends Container<Env> {
  defaultPort = PORT;
  sleepAfter: string | number = "30m";
  entrypoint = ["tessera", "cloud-run", "--port", String(PORT)];

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.envVars = containerEnv(env);
    if (typeof env.SLEEP_AFTER === "string" && env.SLEEP_AFTER) this.sleepAfter = env.SLEEP_AFTER;
  }

  override onStart(): void {
    console.log(JSON.stringify({ event: "tessera.container.started" }));
  }

  override onStop(params: { exitCode?: number; reason?: string }): void {
    console.log(JSON.stringify({ event: "tessera.container.stopped", ...params }));
  }

  override onError(error: unknown): void {
    console.error(JSON.stringify({ event: "tessera.container.error", error: String(error) }));
  }
}

async function ensureStarted(stub: DurableObjectStub<TesseraContainer>, ctx: ExecutionContext): Promise<boolean> {
  const state = await stub.getState();
  if (state.status === "healthy") return true;
  // Start (or keep waiting for) the container in the background; a restore from R2 can take a minute.
  ctx.waitUntil(
    stub.startAndWaitForPorts({ ports: PORT, cancellationOptions: { portReadyTimeoutMS: 10 * 60_000 } }).catch((e: unknown) =>
      console.error(JSON.stringify({ event: "tessera.start.failed", error: String(e) })),
    ),
  );
  return false;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const stub = getContainer(env.TESSERA, INSTANCE);

    if (url.pathname === "/__tessera/status") {
      const state = await stub.getState();
      return Response.json({ container: state.status }, { headers: { "cache-control": "no-store" } });
    }
    if (!(await ensureStarted(stub, ctx))) return starting(request);
    try {
      return await stub.fetch(forwardRequest(request));
    } catch (e) {
      console.error(JSON.stringify({ event: "tessera.proxy.failed", path: url.pathname, error: String(e) }));
      return starting(request);
    }
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    if ((env.KEEP_WARM ?? "true").toLowerCase() === "false") return;
    const stub = getContainer(env.TESSERA, INSTANCE);
    if (await ensureStarted(stub, ctx)) {
      // Any request renews the container's activity timer; /api/ready also checks the databases.
      const r = await stub.fetch(new Request(`http://tessera/api/ready`));
      if (!r.ok) console.error(JSON.stringify({ event: "tessera.ready.failed", status: r.status }));
    }
  },
} satisfies ExportedHandler<Env>;
