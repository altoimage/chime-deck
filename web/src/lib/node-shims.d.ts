/**
 * Minimal ambient types for the Node built-ins the loopback websocket test uses.
 *
 * `@types/node` is deliberately not a dependency of this package — the UI has no
 * other node code, and this ticket was allowed exactly one new dependency. The
 * test files are part of the svelte-check run, though, so the handful of
 * members `ws.realsocket.test.ts` touches are declared here and nothing else.
 *
 * Delete this file the day `@types/node` lands.
 */

declare module 'node:crypto' {
  interface Hash {
    update(data: string): Hash;
    digest(encoding: 'base64'): string;
  }

  export function createHash(algorithm: string): Hash;
}

declare module 'node:net' {
  export interface Socket {
    write(data: Uint8Array | string): boolean;
    destroy(): void;
    on(event: 'data', listener: (chunk: Uint8Array) => void): Socket;
    on(event: 'error', listener: (error: Error) => void): Socket;
    on(event: 'close', listener: () => void): Socket;
  }
}

declare module 'node:http' {
  export interface IncomingMessage {
    headers: Record<string, string | string[] | undefined>;
  }

  export interface ServerResponse {
    writeHead(status: number, headers?: Record<string, string>): void;
    end(body?: string): void;
  }

  export interface AddressInfo {
    address: string;
    port: number;
  }

  export interface Server {
    on(
      event: 'upgrade',
      listener: (request: IncomingMessage, socket: import('node:net').Socket) => void
    ): Server;
    on(event: 'error', listener: (error: Error) => void): Server;
    listen(port: number, host: string, callback: () => void): Server;
    address(): AddressInfo | string | null;
    close(callback?: () => void): Server;
  }

  export function createServer(
    handler: (request: IncomingMessage, response: ServerResponse) => void
  ): Server;
}
