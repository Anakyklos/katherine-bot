import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env?.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env?.VITE_SUPABASE_ANON_KEY;

// Web development (Vite dev/preview server) is expected to carry the
// configuration: `import.meta.env.DEV` is a build-time flag injected by
// Vite, not a guess. Missing credentials there is a misconfiguration
// and must fail fast.
//
// A production *web* deploy with missing credentials is a deployment
// error, but the bundle must still boot: it renders the web auth page
// (which will surface the auth failure) — it must NOT be treated as the
// desktop shell. Mode detection lives in `lib/runtimeMode.js` and never
// depends on "credentials are missing" (#334, review B4).
if (!supabaseUrl || !supabaseAnonKey) {
    if (import.meta.env?.DEV) {
        throw new Error('Missing Supabase environment variables');
    }
}

export const supabase = (supabaseUrl && supabaseAnonKey)
    ? createClient(supabaseUrl, supabaseAnonKey)
    : null;
