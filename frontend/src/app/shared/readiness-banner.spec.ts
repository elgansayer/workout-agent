import { TestBed } from '@angular/core/testing';
import { ReadinessBanner } from './readiness-banner';

const setup = {
  phase: 'setup_required',
  message: 'Connect Hevy to import your routines and training history.',
  next_action: '/settings',
  data: { available: false, verified: false, reason: 'No source.' },
  provider: { available: false, verified: false, reason: 'No key.' },
  setup_steps: [
    { key: 'connect_source', label: 'Connect a training data source', complete: false, action: '/settings' },
  ],
};

describe('ReadinessBanner', () => {
  it('shows the setup steps and action when a capability is missing', async () => {
    await TestBed.configureTestingModule({ imports: [ReadinessBanner] }).compileComponents();
    const fixture = TestBed.createComponent(ReadinessBanner);
    fixture.componentRef.setInput('readiness', setup);
    fixture.detectChanges();

    const text = (fixture.nativeElement as HTMLElement).textContent ?? '';
    expect(text).toContain('Finish setting up');
    expect(text).toContain('Connect a training data source');
    expect((fixture.nativeElement as HTMLElement).querySelector('a.btn')?.getAttribute('href')).toBe(
      '/settings',
    );
  });

  it('renders nothing when the product is ready', async () => {
    await TestBed.configureTestingModule({ imports: [ReadinessBanner] }).compileComponents();
    const fixture = TestBed.createComponent(ReadinessBanner);
    fixture.componentRef.setInput('readiness', {
      ...setup,
      phase: 'ready',
      data: { available: true, verified: true },
      provider: { available: true, verified: true },
    });
    fixture.detectChanges();

    expect((fixture.nativeElement as HTMLElement).querySelector('section')).toBeNull();
  });
});
