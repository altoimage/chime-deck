/**
 * Acceptance criterion at the real transport layer: a real server dies, the
 * tiles go stale within a second, and the client reconnects on its own to the
 * server that came back on the same port.
 *
 * No new dependency: the server below is the smallest RFC 6455 endpoint that
 * the platform `WebSocket` accepts — a 101 handshake (`Sec-WebSocket-Accept` is
 * base64(sha1(key + GUID))) and unmasked server-to-client text frames. It binds
 * to 127.0.0.1 only, so the suite stays offline.
 */

import { createHash } from 'node:crypto';
import { createServer, type Server } from 'node:http';
import type { Socket } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';

import { SNAPSHOT_FRAME } from './fixtures/wire.js';
import { liveState } from './stores.svelte.js';
import { WsClient } from './ws.js';

const WEBSOCKET_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11';

interface WsServer {
  readonly port: number;
  readonly url: string;
  readonly connections: number;
  send(text: string): void;
  stop(): Promise<void>;
}

/** One unmasked, final text frame, as a server sends it. */
function textFrame(text: string): Uint8Array {
  const payload = new TextEncoder().encode(text);
  let headerLength = 2;
  if (payload.length >= 0x10000) {
    headerLength = 10;
  } else if (payload.length >= 126) {
    headerLength = 4;
  }
  const frame = new Uint8Array(headerLength + payload.length);
  frame[0] = 0x81;
  if (headerLength === 2) {
    frame[1] = payload.length;
  } else if (headerLength === 4) {
    frame[1] = 126;
    new DataView(frame.buffer).setUint16(2, payload.length);
  } else {
    frame[1] = 127;
    new DataView(frame.buffer).setBigUint64(2, BigInt(payload.length));
  }
  frame.set(payload, headerLength);
  return frame;
}

function startWsServer(port = 0): Promise<WsServer> {
  return new Promise((resolve, reject) => {
    const sockets = new Set<Socket>();
    let connections = 0;

    const server: Server = createServer((_request, response) => {
      response.writeHead(426, { 'content-type': 'text/plain' });
      response.end('websocket only');
    });

    const send = (text: string): void => {
      const frame = textFrame(text);
      for (const socket of sockets) {
        socket.write(frame);
      }
    };

    server.on('upgrade', (request, socket) => {
      const key = request.headers['sec-websocket-key'];
      if (typeof key !== 'string') {
        socket.destroy();
        return;
      }
      const accept = createHash('sha1').update(key + WEBSOCKET_GUID).digest('base64');
      socket.write(
        'HTTP/1.1 101 Switching Protocols\r\n' +
          'Upgrade: websocket\r\n' +
          'Connection: Upgrade\r\n' +
          `Sec-WebSocket-Accept: ${accept}\r\n\r\n`
      );
      sockets.add(socket);
      connections += 1;
      // Client frames are ignored, exactly like the backend's receive loop.
      socket.on('data', () => undefined);
      socket.on('error', () => undefined);
      socket.on('close', () => sockets.delete(socket));
      send(JSON.stringify(SNAPSHOT_FRAME));
    });

    server.on('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const address = server.address();
      if (address === null || typeof address === 'string') {
        reject(new Error('the test server reported no port'));
        return;
      }
      resolve({
        port: address.port,
        url: `ws://127.0.0.1:${address.port}/ws`,
        get connections(): number {
          return connections;
        },
        send,
        stop: async () => {
          for (const socket of sockets) {
            socket.destroy();
          }
          sockets.clear();
          await new Promise<void>((closed) => server.close(() => closed()));
        }
      });
    });
  });
}

/** Bounded poll: the socket's close is delivered by the platform, not by us. */
async function waitUntil(check: () => boolean, timeoutMs: number): Promise<boolean> {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (check()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  return check();
}

let client: WsClient | null = null;
let server: WsServer | null = null;

afterEach(async () => {
  client?.stop();
  client = null;
  if (server !== null) {
    await server.stop();
    server = null;
  }
  liveState.reset();
});

describe('real websocket', () => {
  it('goes stale within a second of a kill and reconnects by itself', async () => {
    const first = await startWsServer();
    server = first;
    // Only the URL resolver is injected: the port is not known until the
    // server listens. The socket itself is the platform `WebSocket`.
    const url = first.url;
    client = new WsClient(liveState, {
      url: () => url,
      baseDelayMs: 50,
      maxDelayMs: 200,
      view: null,
      net: null
    });
    client.start();

    expect(await waitUntil(() => liveState.stale === false, 3000)).toBe(true);
    expect(liveState.hasSnapshot).toBe(true);
    expect(liveState.entities).toHaveLength(2);

    const port = first.port;
    server = null;
    const killedAt = Date.now();
    await first.stop();
    const wentStale = await waitUntil(() => liveState.stale, 1000);
    const staleDelta = Date.now() - killedAt;

    expect(wentStale).toBe(true);
    expect(staleDelta).toBeLessThan(1000);

    const revived = await startWsServer(port);
    server = revived;
    const recovered = await waitUntil(
      () => revived.connections > 0 && liveState.stale === false,
      5000
    );

    expect(recovered).toBe(true);
    expect(liveState.hasSnapshot).toBe(true);
  }, 15000);
});
