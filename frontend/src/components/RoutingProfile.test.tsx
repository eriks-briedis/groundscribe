/**
 * Choosing which models a project runs against (phase 15).
 *
 * The control is small and the ways to get it subtly wrong are not, so the tests
 * are about what it says as much as what it posts:
 *
 * - it posts the path the backend published, never one it built;
 * - "default" is a choice you can make, expressed as `null` rather than a name;
 * - it does not claim to have permitted anything, because it has not.
 *
 * The last one is the reason this panel is tested at the wording level at all. A
 * person who reads "switched to openai" as "openai may now read my source" has
 * been told something false by an interface that only changed a route.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { RoutingProfilePanel } from './RoutingProfile';
import { fakeBackend } from '@/test/backend';
import { dashboard, PROJECT_ID } from '@/test/fixtures';

const routing = dashboard.routing;
const onDefault = { ...routing, selected: null, policy_version: '11' };
const onOpenAI = { ...routing, selected: 'openai', policy_version: '11-openai' };

describe('choosing a routing profile', () => {
  it('says what the project is running, and which policy that is', () => {
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onOpenAI} actor="ada" />);

    expect(screen.getByTestId('routing-selected')).toHaveTextContent('openai');
    expect(screen.getByText(/11-openai/)).toBeInTheDocument();
  });

  it('calls the shipped policy "Default", having no name to call it by', () => {
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);

    expect(screen.getByTestId('routing-selected')).toHaveTextContent('Default');
  });

  it('offers every profile the backend listed, and the default besides', () => {
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);

    expect(screen.getByRole('button', { name: 'Default' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'openai' })).toBeInTheDocument();
  });

  it('posts the path the backend published, not one it assembled', async () => {
    // The whole point of the published link. A panel that built
    // `/projects/${id}/routing-profile` would be holding its own copy of the
    // routing table, and the copy is the one that goes stale.
    const backend = fakeBackend({ [`/projects/${PROJECT_ID}/routing-profile`]: {} });

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: 'openai' }));

    expect(backend.commands).toHaveLength(1);
    expect(backend.commands[0]?.path).toBe(routing.command?.path);
    expect(backend.commands[0]?.method).toBe('PUT');
  });

  it('sends the profile by name, with whoever chose it', async () => {
    const backend = fakeBackend({ [`/projects/${PROJECT_ID}/routing-profile`]: {} });

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: 'openai' }));

    expect(backend.commands[0]?.body).toEqual({ profile: 'openai', actor_id: 'ada' });
  });

  it('sends null for the default, because that is what the default is', async () => {
    // Not the string "default": the shipped policy's identity is having no name,
    // and a client that invented one would be naming a profile the backend would
    // then go looking for a file for.
    const backend = fakeBackend({ [`/projects/${PROJECT_ID}/routing-profile`]: {} });

    render(<RoutingProfilePanel profiles={onOpenAI} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: 'Default' }));

    expect(backend.commands[0]?.body).toEqual({ profile: null, actor_id: 'ada' });
  });

  it('does not offer to choose what is already chosen', () => {
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onOpenAI} actor="ada" />);

    expect(screen.getByRole('button', { name: 'openai' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Default' })).toBeEnabled();
  });

  it('tells the screen around it to re-read, rather than deciding what changed', async () => {
    // The panel knows a command succeeded. What the project now looks like is the
    // backend's answer, and asking for it again is cheaper than being wrong.
    fakeBackend({ [`/projects/${PROJECT_ID}/routing-profile`]: {} });
    let reloaded = 0;

    render(
      <RoutingProfilePanel profiles={onDefault} actor="ada" onChanged={() => (reloaded += 1)} />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'openai' }));

    expect(reloaded).toBe(1);
  });

  it('shows what the backend said when a choice is refused', async () => {
    fakeBackend({
      [`/projects/${PROJECT_ID}/routing-profile`]: () =>
        new Response(JSON.stringify({ detail: "no routing profile 'openai' (available: none)" }), {
          status: 422,
          headers: { 'content-type': 'application/json' },
        }),
    });

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);
    await userEvent.click(screen.getByRole('button', { name: 'openai' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/no routing profile/i);
  });

  it('does not claim to have permitted the provider, because it has not', () => {
    // Two decisions, and this control makes one. A person who read this as
    // consent would believe their source had been cleared to leave the machine
    // by a button that only changed a route.
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onOpenAI} actor="ada" />);

    expect(screen.getByText(/does not permit its provider/i)).toBeInTheDocument();
  });

  it('says the change applies forward, not backwards', () => {
    fakeBackend({});

    render(<RoutingProfilePanel profiles={onDefault} actor="ada" />);

    expect(screen.getByText(/next stage that runs/i)).toBeInTheDocument();
  });
});
