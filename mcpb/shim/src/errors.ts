/**
 * Thrown by startup-sequence checks (daemon launch timeout) that should
 * exit the process with a specific code. Same reasoning as bridge/src/
 * errors.ts's BridgeExitError: Node has no equivalent of
 * pytest.raises(SystemExit) for unit-testing a bare process.exit() call, so
 * these are raised as a normal error instead and turned into the real
 * process.exit(code) once, at the top of index.ts.
 */
export class ShimExitError extends Error {
  constructor(
    message: string,
    readonly code: number
  ) {
    super(message);
    this.name = "ShimExitError";
  }
}
