/**
 * `longPressDrag` — the D26 touch-native reorder primitive.
 *
 * A drag starts only after the pointer has been held still for
 * LONG_PRESS_MS: if the pointer moves more than 8px before activation the
 * gesture is treated as a scroll/swipe and cancelled, and releasing early is
 * a plain tap — a tap must never start a drag. Once active, the node carries
 * a `data-dragging` attribute and onstart/onmove/onend report start + current
 * coordinates.
 *
 * The caller MUST set `touch-action: none` on the handle so the browser does
 * not claim the gesture for scrolling, and MUST provide the always-visible
 * ▲/▼ button fallback (D26). Not covered by automated tests — behaviour must
 * be finger-verified on the real panel.
 */

import type { Action, ActionReturn } from 'svelte/action';

export const LONG_PRESS_MS = 400;
const CANCEL_DISTANCE_PX = 8;

export interface DragCoordinates {
  startX: number;
  startY: number;
  x: number;
  y: number;
}

export interface LongPressDragParams {
  onstart?: (coordinates: DragCoordinates) => void;
  onmove?: (coordinates: DragCoordinates) => void;
  onend?: (coordinates: DragCoordinates) => void;
}

export const longPressDrag: Action<HTMLElement, LongPressDragParams | undefined> = (
  node,
  params
) => {
  let pointerId: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let lastX = 0;
  let lastY = 0;

  function coordinates(): DragCoordinates {
    return { startX, startY, x: lastX, y: lastY };
  }

  function cancelTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function releaseCapture(): void {
    if (pointerId !== null && node.hasPointerCapture(pointerId)) {
      node.releasePointerCapture(pointerId);
    }
  }

  /** End the gesture. Fires onend only if a drag actually activated. */
  function finish(): void {
    cancelTimer();
    releaseCapture();
    if (dragging) {
      dragging = false;
      node.removeAttribute('data-dragging');
      params?.onend?.(coordinates());
    }
    pointerId = null;
  }

  function onPointerDown(event: PointerEvent): void {
    if (pointerId !== null) {
      return; // already tracking a pointer — ignore extra touches
    }
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    lastX = startX;
    lastY = startY;
    dragging = false;
    node.setPointerCapture(pointerId);
    timer = setTimeout(() => {
      timer = null;
      dragging = true;
      node.setAttribute('data-dragging', '');
      params?.onstart?.(coordinates());
    }, LONG_PRESS_MS);
  }

  function onPointerMove(event: PointerEvent): void {
    if (event.pointerId !== pointerId) {
      return;
    }
    lastX = event.clientX;
    lastY = event.clientY;
    if (!dragging) {
      const moved = Math.hypot(event.clientX - startX, event.clientY - startY);
      if (moved > CANCEL_DISTANCE_PX) {
        // Moved before the long-press elapsed: a scroll or sloppy tap, not a drag.
        finish();
      }
      return;
    }
    params?.onmove?.(coordinates());
  }

  function onPointerUp(event: PointerEvent): void {
    if (event.pointerId !== pointerId) {
      return;
    }
    finish();
  }

  function onPointerCancel(event: PointerEvent): void {
    if (event.pointerId !== pointerId) {
      return;
    }
    finish();
  }

  node.addEventListener('pointerdown', onPointerDown);
  node.addEventListener('pointermove', onPointerMove);
  node.addEventListener('pointerup', onPointerUp);
  node.addEventListener('pointercancel', onPointerCancel);

  const result: ActionReturn<LongPressDragParams | undefined> = {
    update(next) {
      params = next;
    },
    destroy() {
      cancelTimer();
      releaseCapture();
      node.removeAttribute('data-dragging');
      node.removeEventListener('pointerdown', onPointerDown);
      node.removeEventListener('pointermove', onPointerMove);
      node.removeEventListener('pointerup', onPointerUp);
      node.removeEventListener('pointercancel', onPointerCancel);
    }
  };
  return result;
};
