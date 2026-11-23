import { configurePrivateSession } from "/opt/pi/privacy-runtime.ts";

// Child sessions reuse the parent's registered model runtime.
export default configurePrivateSession;
