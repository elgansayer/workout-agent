import { computed, signal } from '@angular/core';

/** Client-side browsing of an already loaded collection; never truncates the source. */
export class PagedList<T> {
  readonly query = signal('');
  private readonly page = signal(1);
  readonly filtered;
  readonly pageCount;
  readonly currentPage;
  readonly start;
  readonly end;
  readonly visible;

  constructor(
    source: () => readonly T[],
    searchText: (item: T) => string,
    readonly pageSize = 8,
  ) {
    this.filtered = computed(() => {
      const query = this.query().trim().toLocaleLowerCase();
      return source().filter(
        (item) => !query || searchText(item).toLocaleLowerCase().includes(query),
      );
    });
    this.pageCount = computed(() => Math.max(1, Math.ceil(this.filtered().length / this.pageSize)));
    this.currentPage = computed(() => Math.min(this.page(), this.pageCount()));
    this.start = computed(() =>
      this.filtered().length ? (this.currentPage() - 1) * this.pageSize + 1 : 0,
    );
    this.end = computed(() => Math.min(this.currentPage() * this.pageSize, this.filtered().length));
    this.visible = computed(() =>
      this.filtered().slice(this.start() ? this.start() - 1 : 0, this.end()),
    );
  }

  search(query: string): void {
    this.query.set(query);
    this.reset();
  }
  reset(): void {
    this.page.set(1);
  }
  previous(): void {
    this.page.set(Math.max(1, this.currentPage() - 1));
  }
  next(): void {
    this.page.set(Math.min(this.pageCount(), this.currentPage() + 1));
  }
}
