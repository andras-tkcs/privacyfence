/**
 * Thrown by startup-sequence checks (daemon launch timeout, version
 * mismatch) that in bridge_main.py call sys.exit(N) directly. Node has no
 * equivalent of pytest.raises(SystemExit) for unit-testing a bare
 * process.exit() call, so these are raised as a normal error instead and
 * turned into the real process.exit(code) once, at the top of index.ts —
 * see main()'s catch handler.
 */
export class BridgeExitError extends Error {
  constructor(
    message: string,
    readonly code: number
  ) {
    super(message);
    this.name = "BridgeExitError";
  }
}
