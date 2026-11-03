import { proxyToApi } from "../../../lib/api-proxy";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";
type Context = { params: Promise<{path: string[]}> };
async function handle(request: Request, context: Context) {
  const {path} = await context.params;
  return proxyToApi(request, `/api/foundry/${path.join("/")}`);
}
export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
