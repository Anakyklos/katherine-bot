import { Auth } from '@supabase/auth-ui-react';
import { ThemeSupa } from '@supabase/auth-ui-shared';
import { supabase } from '../../lib/supabaseClient';
import { isDesktopShell } from '../../lib/runtimeMode';

const Shell = ({ children }) => (
    <div className="min-h-screen bg-[#0a0a0a] flex items-center justify-center p-4">
        <div className="w-full max-w-md bg-[#1a1a1a] p-8 rounded-2xl shadow-2xl border border-white/5">
            <div className="text-center mb-8">
                <h1 className="text-3xl font-light text-white mb-2 tracking-wide">Katherine</h1>
                <p className="text-white/40 text-sm">Sua companheira emocional</p>
            </div>
            {children}
        </div>
    </div>
);

const DesktopNotice = () => (
    <Shell>
        <div className="space-y-4 text-center">
            <p className="text-white/70 text-sm leading-relaxed">
                Modo desktop local: a autenticação web não está configurada
                neste ambiente.
            </p>
            <p className="text-white/40 text-xs leading-relaxed">
                A interface carregou a partir do build local (file://), sem
                servidor HTTP. Este aviso é esperado no shell desktop (#334).
            </p>
        </div>
    </Shell>
);

const WebConfigNotice = () => (
    <Shell>
        <div className="space-y-4 text-center">
            <p className="text-white/70 text-sm leading-relaxed">
                A autenticação não está configurada nesta implantação web.
            </p>
            <p className="text-white/40 text-xs leading-relaxed">
                As variáveis VITE_SUPABASE_URL e VITE_SUPABASE_ANON_KEY
                precisam estar presentes no build web. Este aviso indica
                uma configuração faltando, não o modo desktop (#334).
            </p>
        </div>
    </Shell>
);

const AuthPage = () => {
    // The desktop shell (positively detected via the pywebview bridge
    // global, not via missing credentials — see lib/runtimeMode.js)
    // cannot use the web auth flow, so it renders a local notice.
    // A web deployment without credentials is still `web`: it renders
    // a web configuration notice, never the desktop notice (#334, B4).
    if (isDesktopShell()) {
        return <DesktopNotice />;
    }

    // Web mode: the auth component requires a configured Supabase
    // client. A misconfigured deploy (missing credentials) must surface
    // a clear configuration message instead of crashing on null.auth —
    // and it must never masquerade as the desktop shell.
    if (!supabase) {
        return <WebConfigNotice />;
    }
    return (
        <Shell>
            <Auth
                supabaseClient={supabase}
                appearance={{
                    theme: ThemeSupa,
                    variables: {
                        default: {
                            colors: {
                                brand: '#ffffff',
                                brandAccent: '#e5e5e5',
                                inputBackground: '#262626',
                                inputText: '#ffffff',
                                inputBorder: '#404040',
                                inputLabelText: '#a3a3a3',
                            },
                            radii: {
                                borderRadiusButton: '8px',
                                inputBorderRadius: '8px',
                            },
                        },
                    },
                    className: {
                        button: 'font-normal',
                        input: 'font-light',
                    }
                }}
                providers={[]}
                theme="dark"
                localization={{
                    variables: {
                        sign_in: {
                            email_label: 'Email',
                            password_label: 'Senha',
                            button_label: 'Entrar',
                            loading_button_label: 'Entrando...',
                            email_input_placeholder: 'Seu email',
                            password_input_placeholder: 'Sua senha',
                            link_text: 'Já tem uma conta? Entre',
                        },
                        sign_up: {
                            email_label: 'Email',
                            password_label: 'Senha',
                            button_label: 'Criar conta',
                            loading_button_label: 'Criando conta...',
                            email_input_placeholder: 'Seu email',
                            password_input_placeholder: 'Sua senha',
                            link_text: 'Não tem uma conta? Cadastre-se',
                        },
                    },
                }}
            />
        </Shell>
    );
};

export default AuthPage;
