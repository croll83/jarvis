/**
 * Builds an abort error, using the AbortSignal's reason if available.
 *
 * @param abortSignal - Optional AbortSignal that may contain a reason.
 * @returns An Error with name "AbortError". If the signal has a reason that's
 *          already an Error, returns it directly. Otherwise creates a new Error
 *          with the reason as the message, or "Request aborted" if no reason.
 */
export declare function buildAbortError(abortSignal?: unknown): Error;
