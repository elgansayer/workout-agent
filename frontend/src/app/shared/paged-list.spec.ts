import { signal } from '@angular/core';
import { PagedList } from './paged-list';

describe('PagedList', () => {
  it('keeps every source item reachable and resets the page when searching', () => {
    const source = signal(Array.from({ length: 23 }, (_, i) => `Exercise ${i + 1}`));
    const list = new PagedList(source, (item) => item, 6);
    const seen: string[] = [];
    for (let page = 1; page <= list.pageCount(); page++) {
      seen.push(...list.visible());
      list.next();
    }
    expect(seen).toEqual(source());
    expect(list.currentPage()).toBe(4);
    list.search(' EXERCISE 2 ');
    expect(list.currentPage()).toBe(1);
    expect(list.visible()).toEqual([
      'Exercise 2',
      'Exercise 20',
      'Exercise 21',
      'Exercise 22',
      'Exercise 23',
    ]);
    expect(source()).toHaveLength(23);
  });

  it('clamps pages after refresh and gives an honest empty range', () => {
    const source = signal(['a', 'b', 'c']);
    const list = new PagedList(source, (item) => item, 2);
    list.next();
    source.set(['a']);
    expect(list.currentPage()).toBe(1);
    expect(list.visible()).toEqual(['a']);
    list.search('missing');
    expect([list.start(), list.end(), list.pageCount()]).toEqual([0, 0, 1]);
    list.previous();
    expect(list.currentPage()).toBe(1);
  });
});
