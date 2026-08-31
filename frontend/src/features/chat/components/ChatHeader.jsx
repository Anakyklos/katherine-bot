import React, { useState, useRef, useEffect } from 'react';
import { Eraser, Check, X } from 'lucide-react';
import { checkDesktopHealth } from '../../../lib/desktopBridge';

const ChatHeader = ({ clearScreen }) => {
    const [showConfirm, setShowConfirm] = useState(false);
    const clearButtonRef = useRef(null);
    const prevShowConfirm = useRef(showConfirm);
    const [shouldFocusClearButton, setShouldFocusClearButton] = useState(false);
    const [desktopBridge, setDesktopBridge] = useState(null);

    useEffect(() => {
        // Desktop shell probe (#334): shows the JS->Python->JS round trip.
        // Stays null (hidden) in web mode; never blocks the UI.
        let active = true;
        checkDesktopHealth().then((health) => {
            if (active) {
                setDesktopBridge(health);
            }
        });
        return () => {
            active = false;
        };
    }, []);

    useEffect(() => {
        // Focus restoration when confirmation closes
        if (prevShowConfirm.current && !showConfirm && clearButtonRef.current) {
            clearButtonRef.current.focus();
        }
        prevShowConfirm.current = showConfirm;
    }, [showConfirm]);

    useEffect(() => {
        // Handle Escape key globally when confirmation is open
        const handleKeyDown = (e) => {
            if (e.key === 'Escape') {
                setShowConfirm(false);
            }
        };

        if (showConfirm) {
            document.addEventListener('keydown', handleKeyDown);
        }
        return () => document.removeEventListener('keydown', handleKeyDown);
    }, [showConfirm]);

    const handleClear = () => {
        clearScreen();
        setShowConfirm(false);
    };

    const handleClearClick = () => {
        setShouldFocusClearButton(true);
        setShowConfirm(true);
    };

    return (
        <header className="flex-shrink-0 h-16 border-b border-gray-800 flex items-center justify-between px-4 md:px-8 bg-gray-900 z-10">
            <div className="flex items-center gap-3">
                <div className="font-semibold text-lg tracking-tight text-white">
                    Katherine <span className="text-gray-500 font-normal">– SoulMate</span>
                </div>
                {desktopBridge && (
                    <span
                        data-testid="desktop-bridge-indicator"
                        title="Desktop bridge (pywebview) round trip: OK"
                        className="text-xs text-green-400/80 border border-green-400/30 rounded-full px-2 py-0.5"
                    >
                        desktop v{desktopBridge.api_version}
                    </span>
                )}
            </div>

            {showConfirm ? (
                <div
                    className="flex items-center gap-2"
                    role="group"
                    aria-label="Confirmar limpeza da tela"
                >
                    <span
                        className="text-sm text-gray-400 animate-in fade-in duration-200"
                        id="confirm-text"
                    >
                        Limpar a tela? O histórico salvo permanece.
                    </span>
                    <button
                        onClick={handleClear}
                        className="text-gray-400 hover:text-gray-200 transition-colors p-2 rounded-md hover:bg-gray-800 focus-visible:ring-2 focus-visible:ring-gray-400 focus:outline-none"
                        title="Confirmar limpeza da tela"
                        aria-label="Confirmar limpeza da tela"
                        aria-describedby="confirm-text"
                    >
                        <Check size={20} />
                    </button>
                    <button
                        onClick={() => setShowConfirm(false)}
                        autoFocus
                        className="text-gray-500 hover:text-gray-300 transition-colors p-2 rounded-md hover:bg-gray-800 focus-visible:ring-2 focus-visible:ring-gray-400 focus:outline-none"
                        title="Cancelar"
                        aria-label="Cancelar"
                    >
                        <X size={20} />
                    </button>
                </div>
            ) : (
                <button
                    onClick={handleClearClick}
                    ref={clearButtonRef}
                    autoFocus={shouldFocusClearButton}
                    className="text-gray-500 hover:text-gray-300 transition-colors p-2 rounded-md hover:bg-gray-800 focus-visible:ring-2 focus-visible:ring-gray-400 focus:outline-none"
                    title="Limpar tela"
                    aria-label="Limpar tela"
                >
                    <Eraser size={20} />
                </button>
            )}
        </header>
    );
};

export default ChatHeader;
