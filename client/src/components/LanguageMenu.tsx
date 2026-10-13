import { LanguagesIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useI18n } from "../i18n/index.ts";
import { LANGUAGE_CHOICES, setLanguage, type LanguageChoice } from "../i18n/language.ts";

/** 每种语言用它自己的写法列出来，不管当前界面是哪种语言都认得出 */
const NATIVE_NAMES = { "zh-CN": "中文", en: "English" } as const;

/** 顶部栏里的语言菜单：跟随浏览器、中文、English。选了马上生效，并记在这个浏览器里。 */
export function LanguageMenu() {
  const { t, choice } = useI18n();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" aria-label={t.header.language} />}>
        <LanguagesIcon data-icon="inline-start" />
        {t.header.languageShort}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{t.header.language}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={choice}
            onValueChange={(value: LanguageChoice) => setLanguage(value)}
          >
            {LANGUAGE_CHOICES.map((value) => (
              <DropdownMenuRadioItem
                key={value}
                value={value}
                closeOnClick
                lang={value === "system" ? undefined : value}
              >
                {value === "system" ? t.header.followBrowser : NATIVE_NAMES[value]}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
