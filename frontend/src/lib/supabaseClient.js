/* global process */
import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env?.VITE_SUPABASE_URL;
const supabaseAnonKey = import.meta.env?.VITE_SUPABASE_ANON_KEY;

// Fail fast only when a Vite dev/preview server is expected to have the
// configuration (web development). Production builds may run without
// variables (e.g. the desktop shell loading the bundle via file://, #334):
// there the app must still boot to its auth screen instead of crashing.
if (!supabaseUrl || !supabaseAnonKey) {
    const isViteServer = typeof process !== 'undefined' && process.env?.NODE_ENV === 'development';
    if (isViteServer) {
        throw new Error('Missing Supabase environment variables');
    }
}

export const supabase = (supabaseUrl && supabaseAnonKey) 
    ? createClient(supabaseUrl, supabaseAnonKey) 
    : null;
