/**
 * Light, dark, or whatever the machine already decided (phase 11).
 *
 * Three choices rather than two. "Light" and "dark" are settings; *system* is
 * the absence of one, and it is the default — a local-first tool has no business
 * overriding a preference the person already expressed to their operating
 * system, and one that starts light on a machine set to dark announces itself as
 * a page rather than an application.
 *
 * The mechanism is one attribute. `system` leaves the root element unmarked, so
 * the stylesheet's `prefers-color-scheme` query is what answers; choosing either
 * theme stamps `data-theme`, which the stylesheet lets win in both directions.
 * The alternative — reading the media query here and writing a resolved theme —
 * would need a listener to keep up with the OS, and would be wrong for the
 * length of time it took to notice.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

export type Theme = 'system' | 'light' | 'dark';

/** Where the choice is kept between visits. */
export const THEME_KEY = 'groundscribe:theme';

const THEMES: readonly Theme[] = ['system', 'light', 'dark'];

interface ThemeState {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeState>({ theme: 'system', setTheme: () => undefined });

function isTheme(value: unknown): value is Theme {
  return typeof value === 'string' && (THEMES as readonly string[]).includes(value);
}

/** The stored choice, or `system` when there is none — or no storage to ask. */
function storedTheme(): Theme {
  try {
    const stored = globalThis.localStorage?.getItem(THEME_KEY);
    return isTheme(stored) ? stored : 'system';
  } catch {
    // Private browsing, a blocked origin, or a test environment without storage.
    // None of those are a reason to fail to render an application.
    return 'system';
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(storedTheme);

  useEffect(() => {
    const root = globalThis.document?.documentElement;
    if (!root) return;
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try {
      globalThis.localStorage?.setItem(THEME_KEY, next);
    } catch {
      // A preference that could not be saved is still a preference for this tab.
    }
  }, []);

  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  return useContext(ThemeContext);
}

const LABELS: Record<Theme, string> = { system: 'Auto', light: 'Light', dark: 'Dark' };

/**
 * The control itself: three states, all visible.
 *
 * A single toggle would have to hide one of the three, and the one it hides is
 * always *system* — the setting most people want and the hardest to get back to
 * once a click has taken it away.
 */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="theme-toggle" role="group" aria-label="Colour theme">
      {THEMES.map((option) => (
        <button
          key={option}
          type="button"
          className="theme-toggle__option"
          aria-pressed={theme === option}
          onClick={() => setTheme(option)}
        >
          {LABELS[option]}
        </button>
      ))}
    </div>
  );
}
