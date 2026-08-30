import { Auth } from '@supabase/auth-ui-react';
import { ThemeSupa } from '@supabase/auth-ui-shared';
import { supabase } from '../../lib/supabaseClient';

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
                servidor HTTP. Este aviso é esperado em builds sem credenciais
                Supabase (#334).
            </p>
        </div>
    </Shell>
);

const AuthPage = () => {
    // Without Supabase credentials (desktop shell, #334) the auth-ui
    // component would crash on null.auth, so render a local notice.
    if (!supabase) {
        return <DesktopNotice />;
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
