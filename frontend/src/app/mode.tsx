/**
 * Editorial or debugging (phase 11).
 *
 * plan/11 → *separate editorial vs debugging modes*, the second half of the
 * trace-overload mitigation. The mode changes nothing about what the app is
 * allowed to do — it changes how much of what it has been given is open when a
 * screen loads.
 *
 * Editorial is the default because the product is an editorial tool. A person
 * comes to read an article; the seventeen layers of provenance behind it are
 * there when they are wanted and folded away when they are not.
 */
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';

export type Mode = 'editorial' | 'debugging';

interface ModeState {
  mode: Mode;
  setMode: (mode: Mode) => void;
  /** Whether raw payloads start open. The one question the mode answers. */
  expanded: boolean;
}

const ModeContext = createContext<ModeState>({
  mode: 'editorial',
  setMode: () => undefined,
  expanded: false,
});

export function ModeProvider({ initial = 'editorial', children }: { initial?: Mode; children: ReactNode }) {
  const [mode, setMode] = useState<Mode>(initial);
  const value = useMemo(() => ({ mode, setMode, expanded: mode === 'debugging' }), [mode]);
  return <ModeContext.Provider value={value}>{children}</ModeContext.Provider>;
}

export function useMode(): ModeState {
  return useContext(ModeContext);
}
