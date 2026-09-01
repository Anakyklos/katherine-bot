import React, { useState } from 'react';
import { isDesktopTransport } from '../chat/services/transportMode';
import { useDesktopPrivacy } from './useDesktopPrivacy';

/**
 * Local privacy operations panel (#336, T020).
 *
 * Desktop only: renders nothing when the transport is not the desktop
 * (bridge) branch — the web app keeps its own account-data flows and
 * must never see this surface.
 *
 * Every operation is destructive and local (SQLite, transactional on the
 * Python side): each one requires an explicit confirmation step. The
 * copy states exactly what is erased, in Portuguese, and never mentions
 * servers or accounts (there are none).
 */

const OPERATIONS = [
    {
        op: 'delete_history',
        label: 'Apagar histórico',
        confirm: 'Apagar toda a conversa salva neste dispositivo?',
    },
    {
        op: 'delete_memories',
        label: 'Apagar memórias',
        confirm: 'Apagar todas as memórias salvas neste dispositivo?',
    },
    {
        op: 'reset_emotional_state',
        label: 'Reiniciar estado emocional',
        confirm: 'Reiniciar o estado emocional para o neutro?',
    },
    {
        op: 'reset_relationship_state',
        label: 'Reiniciar vínculo',
        confirm: 'Reiniciar o vínculo para o estado inicial?',
    },
];

export default function PrivacyPanel({ transport }) {
    const desktop = isDesktopTransport(transport);

    const [confirming, setConfirming] = useState(null);
    const [statusMessage, setStatusMessage] = useState(null);
    const { runOp, pending } = useDesktopPrivacy(transport);

    if (!desktop) {
        return null;
    }

    const handleConfirm = async (operation) => {
        setStatusMessage(null);
        const result = await runOp(operation.op);
        setConfirming(null);
        if (result.ok) {
            setStatusMessage('Operação concluída.');
        } else if (result.message) {
            setStatusMessage(result.message);
        } else {
            setStatusMessage('Erro ao concluir a operação. Tente novamente.');
        }
    };

    return (
        <section
            aria-label="Privacidade local"
            className="text-sm text-gray-300 space-y-3"
            data-testid="privacy-panel"
        >
            <h3 className="text-gray-100 font-medium">Privacidade local</h3>
            <p className="text-xs text-gray-400">
                Estes dados ficam apenas neste dispositivo. Cada ação é
                permanente e pede confirmação.
            </p>

            <ul className="space-y-2">
                {OPERATIONS.map((operation) => (
                    <li key={operation.op}>
                        {confirming === operation.op ? (
                            <div className="space-y-2 p-2 rounded bg-gray-800/60">
                                <p className="text-gray-200">{operation.confirm}</p>
                                <p className="text-xs text-gray-400">Tem certeza?</p>
                                <div className="flex gap-2">
                                    <button
                                        type="button"
                                        onClick={() => handleConfirm(operation)}
                                        disabled={pending !== null}
                                        className="px-3 py-1 rounded bg-red-900/70 hover:bg-red-800 text-white disabled:opacity-50"
                                    >
                                        Confirmar
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setConfirming(null)}
                                        disabled={pending !== null}
                                        className="px-3 py-1 rounded bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-50"
                                    >
                                        Cancelar
                                    </button>
                                </div>
                            </div>
                        ) : (
                            <button
                                type="button"
                                onClick={() => { setConfirming(operation.op); setStatusMessage(null); }}
                                disabled={pending !== null}
                                className="px-3 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-200 disabled:opacity-50"
                            >
                                {operation.label}
                            </button>
                        )}
                    </li>
                ))}
            </ul>

            {statusMessage && (
                <p
                    role="status"
                    className={
                        statusMessage.startsWith('Operação concluída')
                            ? 'text-green-400 text-xs'
                            : 'text-red-400 text-xs'
                    }
                >
                    {statusMessage}
                </p>
            )}
        </section>
    );
}
