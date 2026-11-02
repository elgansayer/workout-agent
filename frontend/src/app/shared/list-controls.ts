import { Component, input } from '@angular/core';
import { PagedList } from './paged-list';

@Component({
  selector: 'app-list-controls',
  standalone: true,
  template: `
    <div class="list-toolbar">
      <label [for]="id() + '-search'">Search {{ label() }}</label>
      <div class="input-row">
        <input
          type="search"
          class="list-search"
          [id]="id() + '-search'"
          [value]="list().query()"
          (input)="list().search($any($event.target).value)"
        />
        <ng-content select="[list-filter]" />
      </div>
    </div>
    <div class="list-pagination" [attr.aria-label]="label() + ' pages'">
      <p class="muted" role="status">
        {{ list().start() }}–{{ list().end() }} of {{ list().filtered().length }} {{ label() }}
      </p>
      @if (list().pageCount() > 1) {
        <div class="pager-buttons">
          <button
            class="btn btn-ghost"
            type="button"
            (click)="list().previous()"
            [disabled]="list().currentPage() === 1"
            [attr.aria-label]="'Previous ' + label() + ' page'"
          >
            Previous
          </button>
          <span class="muted">{{ list().currentPage() }} / {{ list().pageCount() }}</span>
          <button
            class="btn btn-ghost"
            type="button"
            (click)="list().next()"
            [disabled]="list().currentPage() === list().pageCount()"
            [attr.aria-label]="'Next ' + label() + ' page'"
          >
            Next
          </button>
        </div>
      }
    </div>
    <ng-content />
    @if (!list().filtered().length && list().query()) {
      <p class="muted">No matches. Try another search.</p>
    }
  `,
})
export class ListControls {
  readonly list = input.required<PagedList<any>>();
  readonly label = input.required<string>();
  readonly id = input.required<string>();
}
