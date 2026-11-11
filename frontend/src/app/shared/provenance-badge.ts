import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { Provenance, provenanceDetail, provenanceKind, provenanceLabel } from './provenance';

/**
 * Labels a record as imported source data, a user entry/correction, or a derived
 * recommendation. The text label always carries the meaning; colour and border
 * style only reinforce it.
 */
@Component({
  selector: 'app-provenance-badge',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span
    class="prov-badge"
    [class]="'prov-badge prov-' + kind()"
    [attr.title]="detail()"
    [attr.data-provenance]="kind()"
    >{{ label() }}</span
  >`,
})
export class ProvenanceBadge {
  readonly provenance = input<Provenance | null | undefined>(null);

  protected readonly kind = computed(() => provenanceKind(this.provenance()));
  protected readonly label = computed(() => provenanceLabel(this.provenance()));
  protected readonly detail = computed(() => provenanceDetail(this.provenance()));
}
