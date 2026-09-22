// Thin fetch wrapper for our API: adds `Authorization: tma <initData>` and normalises errors.

export class ApiError extends Error {
  constructor(status, code, messageUz) {
    super(messageUz || code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.messageUz = messageUz || null;
  }
}

export function createApi(getInitData, fetchImpl = (...args) => fetch(...args)) {
  async function request(method, path, body) {
    let response;
    try {
      response = await fetchImpl(path, {
        method,
        headers: {
          Authorization: `tma ${getInitData()}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, "NETWORK", null);
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      // non-JSON body: fall through to the generic error below
    }
    if (!response.ok) {
      throw new ApiError(response.status, data?.error?.code ?? "HTTP_ERROR", data?.error?.message_uz ?? null);
    }
    return data;
  }

  return {
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body ?? undefined),
  };
}
