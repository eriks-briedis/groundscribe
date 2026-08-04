/**
 * Light, dark, and the machine's own answer (phase 11).
 *
 * The property worth pinning is the default, because it is the one a person
 * never asks for out loud: an application that has not been told which theme to
 * use must not have an opinion, so the root element carries no `data-theme` and
 * the stylesheet's `prefers-color-scheme` query is left to answer.
 *
 * The rest is what makes that survivable — a choice sticks, and choosing *Auto*
 * again gives the decision back to the machine rather than freezing whichever
 * theme happened to be showing.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { THEME_KEY, ThemeProvider, ThemeToggle } from './theme';

function mount() {
  return render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('the colour theme', () => {
  it('defers to the system until somebody says otherwise', () => {
    mount();

    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
    expect(screen.getByRole('button', { name: 'Auto' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('stamps the choice on the root element, where the stylesheet can win with it', async () => {
    mount();

    await userEvent.click(screen.getByRole('button', { name: 'Dark' }));

    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(screen.getByRole('button', { name: 'Dark' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('hands the decision back to the machine when Auto is chosen again', async () => {
    mount();

    await userEvent.click(screen.getByRole('button', { name: 'Light' }));
    await userEvent.click(screen.getByRole('button', { name: 'Auto' }));

    // Not "light, but we stopped listening": the attribute is gone, so a laptop
    // that switches to dark at sunset takes the app with it.
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false);
  });

  it('remembers the choice for the next visit', async () => {
    const first = mount();
    await userEvent.click(screen.getByRole('button', { name: 'Dark' }));
    first.unmount();

    expect(localStorage.getItem(THEME_KEY)).toBe('dark');

    mount();
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  });
});
