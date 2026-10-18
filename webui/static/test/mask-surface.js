"use strict";

// Used only by /test. The server still receives the original image-coordinate
// strokes; this class only accelerates their on-screen preview.
const TILE = 128;
const HISTORY_BYTES = 32 * 1024 * 1024;

function makeCanvas(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

export class MaskSurface {
  constructor(width, height, base, historyLimit = HISTORY_BYTES) {
    this.mask = makeCanvas(width, height);
    this.context = this.mask.getContext("2d", { willReadFrequently: true });
    this.canvas = makeCanvas(width, height);
    this.canvas.className = "mask-canvas";
    this.canvas.setAttribute("aria-label", "Current editable geographic mask over the map");
    this.overlay = this.canvas.getContext("2d");
    this.base = base;
    this.historyLimit = historyLimit;
    this.history = [];
    this.historyBytes = 0;
    this.current = null;
    this.frame = null;
    this.replace(base);
  }

  replace(image, cachedOverlay = null) {
    this.cancelFrame();
    this.current = null;
    this.history = [];
    this.historyBytes = 0;
    this.base = image;
    this.context.drawImage(image, 0, 0);
    if (cachedOverlay) {
      this.overlay.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.overlay.drawImage(cachedOverlay, 0, 0);
    } else {
      this.render({ x: 0, y: 0, width: this.canvas.width, height: this.canvas.height });
    }
  }

  begin(stroke) {
    this.current = { stroke, drawn: 0, tiles: new Map(), bytes: 0, cached: true };
    this.schedule();
  }

  schedule() {
    if (this.frame !== null) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = null;
      this.flush();
    });
  }

  cancelFrame() {
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
  }

  bounds(points, radius) {
    let x0 = this.canvas.width, y0 = this.canvas.height, x1 = 0, y1 = 0;
    for (const [x, y] of points) {
      x0 = Math.min(x0, x - radius - 2);
      y0 = Math.min(y0, y - radius - 2);
      x1 = Math.max(x1, x + radius + 2);
      y1 = Math.max(y1, y + radius + 2);
    }
    const x = Math.max(0, Math.floor(x0));
    const y = Math.max(0, Math.floor(y0));
    return { x, y, width: Math.max(0, Math.min(this.canvas.width, Math.ceil(x1)) - x),
      height: Math.max(0, Math.min(this.canvas.height, Math.ceil(y1)) - y) };
  }

  capture(rect) {
    const current = this.current;
    if (!current.cached) return;
    for (let y = Math.floor(rect.y / TILE) * TILE; y < rect.y + rect.height; y += TILE) {
      for (let x = Math.floor(rect.x / TILE) * TILE; x < rect.x + rect.width; x += TILE) {
        const key = `${x},${y}`;
        if (current.tiles.has(key)) continue;
        const width = Math.min(TILE, this.canvas.width - x);
        const height = Math.min(TILE, this.canvas.height - y);
        const bytes = width * height * 4;
        while (this.history.length && this.historyBytes + current.bytes + bytes > this.historyLimit) {
          this.historyBytes -= this.history.shift().bytes;
        }
        if (current.bytes + bytes > this.historyLimit) {
          // A single enormous stroke must not defeat the memory cap. Undo of
          // this stroke falls back to replaying the in-memory base and strokes.
          current.tiles.clear();
          current.bytes = 0;
          current.cached = false;
          return;
        }
        current.tiles.set(key, { x, y, pixels: this.context.getImageData(x, y, width, height) });
        current.bytes += bytes;
      }
    }
  }

  paint(stroke, points) {
    const ctx = this.context;
    ctx.strokeStyle = stroke.mode === "erase" ? "#000" : "#fff";
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = stroke.radius * 2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.beginPath();
    points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    if (points.length > 1) ctx.stroke();
    else {
      ctx.arc(points[0][0], points[0][1], stroke.radius, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  flush() {
    const current = this.current;
    if (!current || current.drawn === current.stroke.points.length) return;
    const points = current.stroke.points.slice(Math.max(0, current.drawn - 1));
    const rect = this.bounds(points, current.stroke.radius);
    if (rect.width && rect.height) {
      this.capture(rect);
      this.paint(current.stroke, points);
      this.render(rect);
    }
    current.drawn = current.stroke.points.length;
  }

  render(rect) {
    const pixels = this.context.getImageData(rect.x, rect.y, rect.width, rect.height);
    for (let i = 0; i < pixels.data.length; i += 4) {
      const excluded = pixels.data[i] < 128;
      pixels.data[i] = 95;
      pixels.data[i + 1] = 100;
      pixels.data[i + 2] = 110;
      pixels.data[i + 3] = excluded ? 165 : 0;
    }
    this.overlay.putImageData(pixels, rect.x, rect.y);
  }

  finish() {
    this.cancelFrame();
    if (!this.current) return null;
    this.flush();
    const entry = this.current;
    this.history.push(entry);
    this.historyBytes += entry.bytes;
    // Zero-byte fallback entries are bounded as well.
    while (this.history.length > 300) this.historyBytes -= this.history.shift().bytes;
    this.current = null;
    return entry.stroke;
  }

  replay(strokes) {
    this.replace(this.base);
    for (const stroke of strokes) {
      this.begin(stroke);
      this.finish();
    }
  }

  undo(stroke, remaining) {
    this.finish();
    const entry = this.history.at(-1);
    if (!entry || entry.stroke !== stroke || !entry.cached) {
      this.replay(remaining);
      return;
    }
    this.history.pop();
    this.historyBytes -= entry.bytes;
    for (const { x, y, pixels } of entry.tiles.values()) {
      this.context.putImageData(pixels, x, y);
      this.render({ x, y, width: pixels.width, height: pixels.height });
    }
  }

  commit() {
    this.finish();
    const base = makeCanvas(this.canvas.width, this.canvas.height);
    base.getContext("2d").drawImage(this.mask, 0, 0);
    this.base = base;
    this.history = [];
    this.historyBytes = 0;
  }

  checkpoint() {
    const copy = (source) => {
      const canvas = makeCanvas(source.width, source.height);
      canvas.getContext("2d").drawImage(source, 0, 0);
      return canvas;
    };
    return { base: this.base, mask: copy(this.mask), overlay: copy(this.canvas),
      history: this.history, historyBytes: this.historyBytes };
  }

  rollback(checkpoint) {
    this.replace(checkpoint.mask, checkpoint.overlay);
    this.base = checkpoint.base;
    this.history = checkpoint.history;
    this.historyBytes = checkpoint.historyBytes;
  }
}
