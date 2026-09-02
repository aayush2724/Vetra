/**
 * Server-sent events fan-out for the dashboard.
 *
 * SSE rather than WebSockets: the traffic here is entirely one-way (the server
 * telling browsers that an alert appeared), SSE is plain HTTP so it survives
 * proxies that block upgrades, and browsers reconnect on their own. A dashboard
 * left open on a wall display in a village should not need a WebSocket library
 * or a reconnect strategy to keep working.
 */
export class EventBus {
  constructor({ heartbeatMs = 25000 } = {}) {
    this.clients = new Set();
    // Proxies and phone networks close idle connections; a periodic comment
    // keeps the stream alive without sending anything the client must parse.
    this.heartbeat = setInterval(() => this.#ping(), heartbeatMs);
    this.heartbeat.unref?.();
  }

  addClient(res) {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    });
    res.write(': connected\n\n');
    this.clients.add(res);
    res.on('close', () => this.clients.delete(res));
    return res;
  }

  broadcast(event, data) {
    const frame = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
    for (const client of this.clients) {
      // A browser that closed mid-write must not take down the broadcast loop
      // for every other viewer.
      try { client.write(frame); } catch { this.clients.delete(client); }
    }
  }

  #ping() {
    for (const client of this.clients) {
      try { client.write(': ping\n\n'); } catch { this.clients.delete(client); }
    }
  }

  get clientCount() { return this.clients.size; }

  close() {
    clearInterval(this.heartbeat);
    for (const client of this.clients) { try { client.end(); } catch { /* already gone */ } }
    this.clients.clear();
  }
}
